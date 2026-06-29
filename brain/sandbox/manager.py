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
import time
from dataclasses import dataclass
from typing import Any, Optional

import docker
from docker.errors import APIError, BuildError, ImageNotFound, NotFound

from .. import config
from . import policy as policy_mod

LABEL_PREFIX = "brain"


@dataclass
class RunResult:
    container_id: str
    exit_code: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def ok(self) -> bool:
        return (not self.timed_out) and self.exit_code == 0


class SandboxManager:
    def __init__(self, session: str, ledger, *, base_url: Optional[str] = None):
        self.session = session
        self.ledger = ledger
        base_url = base_url if base_url is not None else config.DOCKER_HOST
        self.client = docker.DockerClient(base_url=base_url) if base_url else docker.from_env()

    # -- labels ----------------------------------------------------------- #
    def _labels(self, capability: Optional[str], extra: Optional[dict] = None) -> dict:
        labels = {
            f"{LABEL_PREFIX}.session": self.session,
            f"{LABEL_PREFIX}.created_at": str(time.time()),
        }
        if capability:
            labels[f"{LABEL_PREFIX}.capability"] = capability
        if extra:
            labels.update(extra)
        return labels

    # -- images ----------------------------------------------------------- #
    def ensure_image(self, image: str) -> None:
        try:
            self.client.images.get(image)
        except ImageNotFound:
            self.client.images.pull(image)

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
        try:
            self.client.images.build(
                fileobj=fileobj,
                custom_context=True,
                tag=tag,
                rm=True,
                forcerm=True,
                pull=False,
            )
        except BuildError as e:  # surface the build log for debugging
            log = "\n".join(
                str(chunk.get("stream", "")).rstrip()
                for chunk in getattr(e, "build_log", [])
                if isinstance(chunk, dict) and chunk.get("stream")
            )
            raise BuildError(f"{e}\n--- build log ---\n{log}", getattr(e, "build_log", None))
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
    ) -> RunResult:
        pol = policy or policy_mod.isolated()
        ttl = ttl if ttl is not None else pol.ttl_seconds
        self.ensure_image(image)

        env = {"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
        if environment:
            env.update(environment)

        container = self.client.containers.create(
            image,
            command,
            detach=True,
            environment=env,
            labels=self._labels(capability, labels),
            working_dir="/work",
            **pol.docker_kwargs(),
        )
        self.ledger.record_start(
            id=container.id,
            session=self.session,
            capability=capability,
            image=image,
            command=command,
            policy=pol,
        )

        timed_out = False
        exit_code: Optional[int] = None
        stdout = stderr = ""
        try:
            container.start()
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
            try:
                stdout = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
                stderr = container.logs(stdout=False, stderr=True).decode("utf-8", "replace")
            except Exception:
                pass
        finally:
            self._remove(container)
            self.ledger.record_finish(
                container.id,
                exit_code=exit_code,
                status="reaped" if timed_out else "finished",
            )

        return RunResult(
            container_id=container.id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )

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

    def close(self) -> None:
        self.reap()
        try:
            self.client.close()
        except Exception:
            pass
