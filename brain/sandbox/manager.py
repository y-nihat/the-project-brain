"""Docker sandbox manager: the only path through which anything executes.

Owns the full container lifecycle for the brain: build images from in-memory
contexts, run short-lived containers under strict resource/network policy with
brain labels, capture their output, and guarantee teardown. Every container is
recorded in the :class:`~brain.ledger.Ledger` so leaks are detectable.

The brain talks to the daemon through the socket-proxy, so we never assume host
paths: build contexts are streamed as tar, inputs are passed via environment,
and outputs are read from stdout. (Larger artifact exchange via archive
streaming is a later-phase extension.)
"""
from __future__ import annotations

import io
import tarfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import docker
from docker.errors import APIError, BuildError, ImageNotFound, NotFound

from .. import config
from ..observability import NullTracer
from . import policy as policy_mod

LABEL_PREFIX = "brain"


def _tail(text: str, limit: int = 2000) -> str:
    """Keep the END of a long text — build/runtime errors live at the tail."""
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _make_tar(files: dict) -> bytes:
    """Pack {member_path: bytes} into a tar for ``container.put_archive``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _extract_tar(raw: bytes, strip_prefix: str = "") -> dict:
    """Unpack a tar (from ``container.get_archive``) into {name: bytes}."""
    out: dict = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            name = member.name
            if strip_prefix and name.startswith(strip_prefix):
                name = name[len(strip_prefix):]
            out[name] = handle.read()
    return out


@dataclass
class RunResult:
    container_id: str
    exit_code: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool
    output_files: dict = field(default_factory=dict)
    oom: bool = False                       # kernel OOM-killed (memory limit exceeded)
    peak_mem_mb: Optional[float] = None     # best-effort observed peak memory
    error_note: Optional[str] = None        # engine-side note (e.g. output cap exceeded)

    @property
    def ok(self) -> bool:
        return (not self.timed_out) and self.exit_code == 0


class SandboxManager:
    def __init__(self, session: str, ledger, *, base_url: Optional[str] = None, tracer=None,
                 conversation: Optional[str] = None):
        self.session = session
        # The conversation this run belongs to. Labeled on every container so a
        # process cleaning up leftovers can tell another live conversation's
        # containers from orphans.
        self.conversation = conversation
        self.ledger = ledger
        self.tracer = tracer or NullTracer()
        base_url = base_url if base_url is not None else config.DOCKER_HOST
        self.client = docker.DockerClient(base_url=base_url) if base_url else docker.from_env()

    # -- labels ----------------------------------------------------------- #
    def _labels(self, capability: Optional[str], extra: Optional[dict] = None) -> dict:
        labels = {
            f"{LABEL_PREFIX}.session": self.session,
            f"{LABEL_PREFIX}.created_at": str(time.time()),
        }
        if self.conversation:
            labels[f"{LABEL_PREFIX}.conversation"] = self.conversation
        if capability:
            labels[f"{LABEL_PREFIX}.capability"] = capability
        if extra:
            labels.update(extra)
        return labels

    # -- images ----------------------------------------------------------- #
    def ensure_image(self, image: str) -> None:
        """Make ``image`` available locally — by pulling it ONLY if it is a base
        image, never if it is an authored capability.

        A capability image is built here and exists only here. If it is missing,
        the honest answer is "rebuild it from its retained build context", not
        "fetch something with that name from a public registry": the tag used to
        be `brain/cap-<name>`, which Docker resolves to docker.io/brain/cap-*, a
        namespace this project does not own. One `docker system prune`, one
        rebuilt VM, one move to a second machine — and an unconditional pull
        would have run a stranger's image as a capability the registry still
        lists as verified, with that capability's declared credential injected
        and its output entering the evidence ledger.
        """
        try:
            self.client.images.get(image)
        except ImageNotFound:
            from .. import config

            name = image.rsplit("/", 1)[-1]
            if name.startswith(config.CAP_IMAGE_PREFIX):
                raise ImageNotFound(
                    f"capability image '{image}' is missing locally. It is built "
                    "here and is never fetched from a registry — rebuild it from "
                    f"data/capabilities/<name>/build/ instead."
                ) from None
            self.client.images.pull(image)

    def tag_image(self, source: str, target: str) -> None:
        """Point ``target`` at the image ``source`` names — the atomic promotion
        step: candidates are built under attempt-scoped tags and only a VERIFIED
        candidate may take over the canonical tag (an unverified attempt was
        observed replacing the trusted local image before promotion)."""
        repo, _, tag = target.rpartition(":")
        self.client.images.get(source).tag(repo, tag or None)

    def remove_image(self, tag: str) -> None:
        """Best-effort: drop an image TAG (the image survives under its other
        tags). Used to clean up candidate aliases after promotion or failure."""
        try:
            self.client.images.remove(tag)
        except Exception:  # noqa: BLE001 — cleanup must never fail a run
            pass

    def build_image(self, tag: str, build_context: dict[str, str]) -> str:
        """Build an image from an in-memory ``{filename: content}`` context.

        The context must include a ``Dockerfile``. Build runs on the daemon (it
        needs egress for base images / pip); the brain only streams the tar.
        """
        if "Dockerfile" not in build_context:
            raise ValueError("build_context must include a 'Dockerfile' entry")
        fileobj = io.BytesIO()
        with tarfile.open(fileobj=fileobj, mode="w") as tar:
            for name, content in build_context.items():
                data = content.encode("utf-8")
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(data))
        fileobj.seek(0)
        from ..observability import activity

        # A build is delegated work an agent session may be silently awaiting —
        # the stall watchdog must not read that silence as a hang.
        with activity.working(), self.tracer.span("image.build", tag, tag=tag):
            try:
                self.client.images.build(
                    fileobj=fileobj,
                    custom_context=True,
                    tag=tag,
                    rm=True,
                    forcerm=True,
                    pull=False,
                    labels={f"{LABEL_PREFIX}.managed": "1"},
                )
            except BuildError as e:
                # Surface the TAIL of the build log in the message (that's where
                # the real error is); keep the full log on the exception for
                # callers that persist it as a run artifact.
                log = "\n".join(
                    str(chunk.get("stream", "")).rstrip()
                    for chunk in getattr(e, "build_log", [])
                    if isinstance(chunk, dict) and chunk.get("stream")
                )
                err = BuildError(
                    f"{e}\n--- build log (tail) ---\n{_tail(log)}",
                    getattr(e, "build_log", None),
                )
                err.full_log = log
                raise err
        return tag

    # -- running ---------------------------------------------------------- #
    def run_ephemeral(
        self,
        image: str,
        command: Any,
        *,
        policy: Optional[policy_mod.ResourcePolicy] = None,
        capability: Optional[str] = None,
        environment: Optional[dict] = None,
        labels: Optional[dict] = None,
        ttl: Optional[int] = None,
        input_files: Optional[dict] = None,
        capture_output_files: bool = False,
        purpose: Optional[str] = None,
    ) -> RunResult:
        pol = policy or policy_mod.isolated()
        ttl = ttl if ttl is not None else pol.ttl_seconds
        from ..observability import activity

        # Sandbox runs execute inside an agent's tool call: the calling
        # session's stream is silent the whole time. Mark the window so the
        # stall watchdog knows waiting is not hanging (a run's ttl can exceed
        # the stall timeout).
        # WHY this container runs. The engine executes a tool for two very
        # different reasons — because an agent asked for its result, or because
        # the engine is JUDGING the tool (verification, fabrication probes,
        # revalidation) — and a judging run's nonzero exit is frequently the
        # PASSING outcome. Without this distinction the live stream reported
        # "ran web_search exit=2" during a healthy revalidation, which reads as
        # a broken tool being used anyway.
        with activity.working(), self.tracer.span(
            "sandbox.run",
            capability or image,
            image=image,
            network=pol.network,
            mem=pol.mem,
            cpus=pol.cpus,
            ttl=ttl,
            **({"purpose": purpose} if purpose else {}),
        ) as sp:
            self.ensure_image(image)

            env = {"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
            if environment:
                env.update(environment)

            files_mode = bool(input_files) or capture_output_files
            kwargs = pol.docker_kwargs()
            if files_mode:
                # Files can't survive a tmpfs / read-only /work, so make /work a
                # normal writable dir in the rootfs to inject and collect files.
                kwargs["read_only"] = False
                kwargs["tmpfs"] = {"/tmp": "size=64m,mode=1777"}

            container = self.client.containers.create(
                image,
                command,
                detach=True,
                environment=env,
                labels=self._labels(capability, labels),
                working_dir="/work",
                **kwargs,
            )
            sp.set(ledger_id=container.id)
            self.ledger.record_start(
                id=container.id,
                session=self.session,
                conversation=self.conversation,
                capability=capability,
                image=image,
                command=command,
                policy=pol,
            )

            timed_out = False
            exit_code: Optional[int] = None
            stdout = stderr = ""
            output_files: dict = {}
            oom = False
            error_note: Optional[str] = None
            peak = {"bytes": 0}
            stop_sampling = threading.Event()

            def _sample_memory() -> None:
                # Best-effort peak-memory observation (~1s cadence). Short runs
                # may record nothing; that's acceptable for accounting.
                while not stop_sampling.is_set():
                    try:
                        stats = container.stats(stream=False)
                        usage = (stats.get("memory_stats") or {}).get("usage") or 0
                        peak["bytes"] = max(peak["bytes"], int(usage))
                    except Exception:
                        return
                    stop_sampling.wait(1.0)

            try:
                if input_files:
                    container.put_archive(
                        "/", _make_tar({f"work/in/{n}": d for n, d in input_files.items()})
                    )
                container.start()
                sampler = threading.Thread(target=_sample_memory, daemon=True)
                sampler.start()
                try:
                    exit_code = container.wait(timeout=ttl).get("StatusCode")
                except Exception:
                    # docker-py raises a requests timeout/connection error past `ttl`
                    timed_out = True
                    exit_code = -1
                    try:
                        container.kill()
                    except Exception:
                        pass
                finally:
                    stop_sampling.set()
                    sampler.join(timeout=2.0)
                try:
                    container.reload()
                    oom = bool((container.attrs.get("State") or {}).get("OOMKilled"))
                except Exception:
                    pass
                if capture_output_files:
                    output_files, error_note = self._capture_output(container)
                try:
                    stdout = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
                    stderr = container.logs(stdout=False, stderr=True).decode("utf-8", "replace")
                except Exception:
                    pass
            finally:
                stop_sampling.set()
                self._remove(container)
                self.ledger.record_finish(
                    container.id,
                    exit_code=exit_code,
                    status="reaped" if timed_out else "finished",
                    peak_mem_mb=round(peak["bytes"] / (1024 * 1024), 1) if peak["bytes"] else None,
                    oom=oom,
                )
            peak_mem_mb = round(peak["bytes"] / (1024 * 1024), 1) if peak["bytes"] else None
            sp.set(exit_code=exit_code, timed_out=timed_out, oom=oom, peak_mem_mb=peak_mem_mb)
            if timed_out or oom or (exit_code not in (0, None)):
                sp.status = "failed"

        return RunResult(
            container_id=container.id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            output_files=output_files,
            oom=oom,
            peak_mem_mb=peak_mem_mb,
            error_note=error_note,
        )

    def _capture_output(self, container) -> tuple[dict, Optional[str]]:
        """Stream /work/out back, bounded so a runaway tool cannot exhaust the
        orchestrator's memory."""
        cap_bytes = config.SANDBOX_MAX_OUTPUT_MB * 1024 * 1024
        try:
            bits, _ = container.get_archive("/work/out")
            buf = bytearray()
            for chunk in bits:
                buf.extend(chunk)
                if len(buf) > cap_bytes:
                    return {}, (
                        f"output exceeded the {config.SANDBOX_MAX_OUTPUT_MB} MB capture cap; "
                        "files were dropped"
                    )
            return _extract_tar(bytes(buf), strip_prefix="out/"), None
        except Exception:
            return {}, None

    # -- teardown --------------------------------------------------------- #
    def _remove(self, container) -> None:
        try:
            container.remove(force=True)
        except (NotFound, APIError):
            pass

    def reap(self) -> list[str]:
        """Force-remove any container still labeled for this session."""
        removed: list[str] = []
        try:
            containers = self.client.containers.list(
                all=True, filters={"label": f"{LABEL_PREFIX}.session={self.session}"}
            )
        except APIError:
            return removed
        for c in containers:
            self._remove(c)
            self.ledger.record_finish(c.id, exit_code=None, status="reaped")
            removed.append(c.id)
        return removed

    def prune_images(self) -> None:
        """Remove *dangling* brain-built image layers (failed or superseded
        builds). Promoted capability images are tagged and therefore kept."""
        try:
            self.client.images.prune(
                filters={"dangling": True, "label": f"{LABEL_PREFIX}.managed=1"}
            )
        except APIError:
            pass

    def close(self) -> None:
        self.reap()
        self.prune_images()
        try:
            self.client.close()
        except Exception:
            pass
