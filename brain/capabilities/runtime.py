"""Expose registered capabilities to agents as in-process MCP tools.

Each capability becomes ``mcp__caps__<name>``. The handler runs the capability in
a fresh, resource-limited sandbox container, and bridges files through a shared
**per-run workspace** so tools can hand files to one another:

  * a reserved ``files`` argument (list of workspace-relative paths) is read from
    the workspace and injected under ``/work/in/`` in the container;
  * anything the tool writes to ``/work/out/`` is captured back into the
    workspace under ``<capability>/`` and its paths are returned to the agent, so
    a downstream agent can pass them as ``files`` to the next tool.

Scalar arguments still go in as environment variables. The agent never touches
Docker or the host.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Iterable, Optional

from .. import evidence as evidence_mod
from ..models import Capability
from ..sandbox import policy
from . import health as health_mod

# Observation results in a modality the reasoning model supports must be
# PRESENTABLE in that modality: an image a tool produced (a rendered page, a
# chart, a frame, a scan) is returned to the agent as an image content block,
# not a "(N bytes, binary)" black hole. Without this, no plan involving visual
# observation is executable, so visual channels silently vanish from the
# strategy space. Bounded hard — perception, not bulk transfer.
_IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}


def _encoded_len(raw_len: int) -> int:
    return (raw_len * 4 + 2) // 3


def _fit_image(data: bytes, mime: str, max_encoded: int):
    """Fit an image under an ENCODED-size ceiling by recompressing and
    downscaling (perception survives at lower resolution; a session killed by
    an oversized frame does not). Returns (bytes, mime) or None when the image
    cannot be made to fit (Pillow missing or floor reached)."""
    if _encoded_len(len(data)) <= max_encoded:
        return data, mime
    try:
        import io

        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        quality = 80
        for _ in range(16):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality)
            out = buf.getvalue()
            if _encoded_len(len(out)) <= max_encoded:
                return out, "image/jpeg"
            if quality > 50:
                quality -= 15
            elif img.width > 480:
                img = img.resize((max(480, int(img.width * 0.7)),
                                  max(1, int(img.height * 0.7))))
            else:
                break
    except Exception:  # noqa: BLE001 — a broken image must not kill the result
        return None
    return None


def image_blocks(produced: list[str], workspace: Path,
                 max_images: int, max_bytes: int,
                 budget_bytes: int = 0) -> list[dict]:
    """MCP image content blocks for image files among a tool's outputs, in
    production order, capped in count, per-file size, and — decisively — in
    TOTAL encoded size: the transport rejects messages over its frame limit,
    so blocks that would overflow ``budget_bytes`` are downscaled to fit and
    dropped only when they cannot be. Per-image caps alone proved fatal:
    individually-legal images jointly exceeded the frame and killed sessions."""
    blocks: list[dict] = []
    remaining = budget_bytes if budget_bytes > 0 else _encoded_len(max_bytes) * max_images
    for rel in produced:
        if len(blocks) >= max_images or remaining < 512:
            break
        mime = _IMAGE_EXTS.get(Path(rel).suffix.lower())
        if not mime:
            continue
        p = Path(workspace) / rel
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if not data or len(data) > max_bytes:
            continue
        fitted = _fit_image(data, mime, remaining)
        if fitted is None:
            continue
        raw, mime = fitted
        b64 = base64.b64encode(raw).decode("ascii")
        remaining -= len(b64) + 256
        blocks.append({"type": "image", "data": b64, "mimeType": mime})
    return blocks


def fit_result_text(text: str, budget: int) -> str:
    """Bound a result's TEXT under the transport budget, keeping head and tail
    (the tail carries the tool's machine-readable meta line). An unbounded
    stdout is the same session-killer as an oversized image."""
    if len(text) <= budget:
        return text
    keep = max(4096, budget - 200)
    head, tail = keep * 2 // 3, keep // 3
    return (text[:head]
            + f"\n[…{len(text) - keep:,} chars omitted — the full result exceeded the "
              "transport-safe size; consume the produced files instead of this preview…]\n"
            + text[-tail:])


def _env_value(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    return json.dumps(v)


def _observe(monitor, tracer, cap_name: str, result_class: str, count: int) -> None:
    """Record a tool result's health and surface it in the trace."""
    if monitor is not None:
        monitor.record(cap_name, result_class)
    if tracer is not None:
        tracer.event("tool.result", cap_name, status=result_class, result=result_class, count=count)


def incompatible_refs(file_refs, accepts: list[str]) -> list[str]:
    """Files whose type the capability does not declare it can process (#11)."""
    if not file_refs or not accepts:
        return []
    ok = tuple(e.lower() if e.startswith(".") else "." + e.lower() for e in accepts)
    return [str(r) for r in file_refs if not str(r).lower().endswith(ok)]


def provenance_line(cap_name: str, file_refs, meta) -> str:
    """Provenance from what the tool REPORTS it observed (#10), not routing.

    Tools' output contract includes `consumed_inputs` (files actually read),
    `source_refs` (remote locators actually accessed) and optionally
    `generated`/`generated_fixtures`. Routed-but-unconsumed files are flagged;
    unreported consumption is marked unverified rather than trusted. A tool
    that genuinely observed a REMOTE source is not 'from arguments only' —
    that wording erased real observations from the evidence model."""
    meta = meta if isinstance(meta, dict) else {}
    generated = bool(meta.get("generated") or meta.get("generated_fixtures"))
    routed = [str(r) for r in (file_refs or [])]
    remote = meta.get("source_refs")
    remote = [str(s)[:120] for s in remote][:6] if isinstance(remote, list) else []
    consumed = meta.get("consumed_inputs")
    if isinstance(consumed, list):
        consumed = [str(c) for c in consumed]
        ignored = [r for r in routed if not any(c in r or r in c for c in consumed)]
        parts = []
        if consumed:
            parts.append(f"derived by '{cap_name}' from consumed input(s): {consumed}")
        elif remote:
            parts.append(f"observed by '{cap_name}' from remote source(s): {remote}")
        else:
            parts.append(f"'{cap_name}' consumed NO input files — result is NOT derived "
                         "from user evidence")
        if consumed and remote:
            parts.append(f"also observed remote source(s): {remote}")
        if ignored:
            parts.append(f"routed but NOT consumed (result is NOT derived from these): {ignored}")
        if generated:
            parts.append("contains GENERATED/FIXTURE content — NON-EVIDENCE, never present it "
                         "as coming from a user document")
        return "; ".join(parts)
    if remote:
        return f"observed by '{cap_name}' from remote source(s): {remote}"
    if routed:
        return (f"routed input(s) {routed} to '{cap_name}', but the tool did not report "
                "consumed_inputs — treat any 'derived from these files' assumption as UNVERIFIED")
    return (f"produced by '{cap_name}' from arguments only (NO input file was read — do not "
            "treat this as content extracted from a user document)")


def _save_outputs(workspace: Path, cap_name: str, output_files: dict) -> list[str]:
    """Write a tool's captured /work/out files into the run workspace.

    Handles nested paths (e.g. ``project/bot.py``) and refuses anything that
    would escape the capability's directory. Returns workspace-relative paths.
    """
    capdir = (workspace / cap_name).resolve()
    capdir.mkdir(parents=True, exist_ok=True)
    produced: list[str] = []
    for name, data in output_files.items():
        dest = (capdir / name).resolve()
        if not str(dest).startswith(str(capdir)):
            continue  # path-traversal guard: tar names come from the container
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        produced.append(f"{cap_name}/{dest.relative_to(capdir)}")
    return produced


def build_caps_server(
    capabilities: Iterable[Capability],
    *,
    manager,
    workspace: Optional[str | Path] = None,
    monitor=None,
    tracer=None,
    secrets: Optional[dict] = None,
    server_name: str = "caps",
):
    """Create an in-process MCP server exposing each capability as a tool.

    ``monitor`` (a :class:`~brain.capabilities.health.HealthMonitor`) observes each
    tool's live results and enforces a per-capability call budget, so the
    orchestrator can repair a degraded tool instead of letting an agent grind it.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    ws = Path(workspace) if workspace else None

    def make(cap: Capability):
        @tool(cap.name, cap.description, cap.input_schema)
        async def handler(args):
            # Bound effort: a degraded tool must not be hammered forever.
            if monitor is not None and monitor.over_budget(cap.name):
                return {
                    "content": [{"type": "text", "text": (
                        f"call budget for '{cap.name}' is exhausted "
                        f"({monitor.call_budget} calls). Stop calling it and conclude "
                        "with what you have already gathered.")}],
                    "is_error": True,
                }

            args = dict(args)
            file_refs = args.pop("files", None)  # reserved: workspace paths to attach

            # Caller errors are not tool failures: a nonexistent path never
            # reaches the container, is answered with the ACTUAL available
            # files, and does not count against the tool's health (agents were
            # observed burning ~10 calls guessing paths a tool misreported).
            if file_refs and ws is not None:
                missing = [str(r) for r in file_refs if not (ws / str(r)).is_file()]
                if missing:
                    allf = sorted(
                        str(p.relative_to(ws)) for p in ws.rglob("*")
                        if p.is_file() and p.relative_to(ws).parts[0] != "prior")
                    # The cap is a DISPLAY limit — say so, so an agent never reads
                    # "first 40 shown" as "only 40 files exist" (a worker concluded
                    # the video had 40 frames when 402 were extracted).
                    more = f" (showing first 40 of {len(allf)})" if len(allf) > 40 else ""
                    return {
                        "content": [{"type": "text", "text": (
                            f"file(s) not found in the workspace: {missing}. The tool was NOT "
                            f"run. Files that actually exist{more}: {allf[:40]}. Use these exact "
                            "workspace-relative paths. If a file the task REQUIRES is not among "
                            "them, it was never provided to this run — report a fatal blocker "
                            "naming it (report_blocker, severity='fatal'); do NOT retry with "
                            "guessed paths and never fabricate its contents.")}],
                        "is_error": True,
                    }

            # Applicability before execution (#11): an incompatible input never
            # reaches the tool, so it never gets the chance to substitute one.
            bad = incompatible_refs(file_refs, cap.accepts)
            if bad:
                return {
                    "content": [{"type": "text", "text": (
                        f"incompatible input for '{cap.name}': {bad} — this tool accepts "
                        f"{cap.accepts}. It was NOT run. Use/author a tool fit for that "
                        "input type, or replan; do not retry with the same input.")}],
                    "is_error": True,
                }

            env = {cap.env_map[k]: _env_value(v) for k, v in args.items() if k in cap.env_map}

            # Session secrets: injected engine-side, never through agent args,
            # never traced. A credentialed tool without its secret fails honestly.
            if cap.requires_credential:
                secret = (secrets or {}).get(cap.requires_credential)
                if not secret:
                    return {
                        "content": [{"type": "text", "text": (
                            f"'{cap.name}' needs the credential {cap.requires_credential}, which was "
                            "not provided in this session. Do not fabricate results; report this "
                            "limitation and what the user must supply.")}],
                        "is_error": True,
                    }
                env[cap.requires_credential] = secret

            input_files = {}
            if file_refs and ws is not None:
                for ref in file_refs:
                    p = ws / str(ref)
                    if p.exists() and p.is_file():
                        input_files[p.name] = p.read_bytes()

            pol = policy.for_capability(cap.memory_mb, cap.cpus, cap.timeout_seconds, cap.network)
            result = await asyncio.to_thread(
                manager.run_ephemeral,
                cap.image_tag,
                cap.command,
                policy=pol,
                capability=cap.name,
                environment=env,
                input_files=input_files or None,
                capture_output_files=True,
            )

            if not result.ok:
                if result.oom:
                    reason = (f"the tool exceeded its memory limit ({pol.mem}) and was killed; "
                              "try a smaller input or report this limitation")
                else:
                    reason = (f"exit={result.exit_code}, timed_out={result.timed_out}: "
                              f"{result.stderr[-800:]}")
                _observe(monitor, tracer, cap.name, health_mod.ERROR, 0)
                return {
                    "content": [{
                        "type": "text",
                        "text": f"capability '{cap.name}' failed — {reason}",
                    }],
                    "is_error": True,
                }

            produced: list[str] = []
            if result.output_files and ws is not None:
                produced = _save_outputs(ws, cap.name, result.output_files)
                # Retention is a LEDGER fact, not narration: record what was
                # kept, the provenance the tool reported, AND the workspace
                # files this call consumed — the edges lineage propagates
                # along, so a property-preserving transformation of the target
                # stays target-lineage whatever role its tool declared.
                try:
                    meta_line = json.loads(result.stdout.strip().splitlines()[-1])
                except Exception:  # noqa: BLE001
                    meta_line = {}
                if not isinstance(meta_line, dict):
                    meta_line = {}
                refs = meta_line.get("source_refs")
                reported = meta_line.get("consumed_inputs")
                routed = [str(r) for r in (file_refs or [])]
                if isinstance(reported, list):
                    names = {str(Path(str(c)).name) for c in reported}
                    consumed = [r for r in routed if Path(r).name in names] or routed
                else:
                    consumed = routed
                evidence_mod.record(
                    ws.parent, cap.name, produced,
                    refs if isinstance(refs, list) else [],
                    str(meta_line.get("coverage", "")),
                    inputs=consumed,
                    state=str(meta_line.get("observation_state", "")),
                    role=cap.emits or "", workspace=ws)
                # Causal-attribution evidence: did this call persist supplied
                # content EXACTLY? A write that matches its input byte-for-byte
                # exonerates the tool from defects IN that content — those
                # belong to whoever generated it.
                if monitor is not None:
                    supplied = [v.strip() for v in args.values()
                                if isinstance(v, str) and len(v) >= 24]
                    if supplied:
                        monitor.note_verbatim(cap.name, all(
                            data.decode("utf-8", errors="ignore").strip() in supplied
                            for data in result.output_files.values()))

            # Observe what actually came back (ok / empty / error) — the silent
            # failure mode is exit 0 with no data.
            rclass, count = health_mod.classify_result(result.stdout)
            if produced and rclass == health_mod.EMPTY:
                rclass, count = health_mod.OK, len(produced)  # producing files is a real result
            _observe(monitor, tracer, cap.name, rclass, count)

            # A buggy tool may echo its own credential; nothing secret-shaped
            # leaves the sandbox boundary toward agents, traces, or files.
            from ..observability.redact import scrub_text

            text = scrub_text(result.stdout.strip()) or "(no stdout)"
            # Provenance from ACTUAL observation (#10): what the tool reports it
            # consumed/generated, with routed-but-ignored inputs flagged.
            meta = None
            try:
                meta = json.loads(result.stdout.strip().splitlines()[-1])
            except Exception:  # noqa: BLE001
                pass
            text = f"[provenance] {provenance_line(cap.name, file_refs, meta)}\n\n{text}"
            if produced:
                text += ("\n\nProduced files (pass these paths as `files` to a downstream tool): "
                         + json.dumps(produced))
            if result.error_note:
                text += f"\n\n[engine note] {result.error_note}"
            if rclass == health_mod.EMPTY:
                text += ("\n\n[engine note] this returned NO results. If this keeps happening the "
                         "tool may be degraded — vary your query or stop and report the limitation.")
            # Evidentiary yield ≠ operational health: an exit-0 result that
            # covers only part of what was asked is a KNOWLEDGE gap, and the
            # agent must route it to the controller, never paper over it.
            coverage = str(meta.get("coverage", "")) if isinstance(meta, dict) else ""
            needs = meta.get("unresolved_needs") if isinstance(meta, dict) else None
            needs = [str(n)[:120] for n in needs][:5] if isinstance(needs, list) else []
            state = str(meta.get("observation_state", "")) if isinstance(meta, dict) else ""
            if coverage in ("partial", "none") or needs:
                if tracer is not None:
                    tracer.event("evidence.gap", cap.name, status="info",
                                 coverage=coverage or "unreported", needs=needs,
                                 state=state or "unreported")
                text += ("\n\n[engine note] evidentiary yield: coverage="
                         + (coverage or "unreported")
                         + (f"; observation_state={state}" if state else "")
                         + (f"; unresolved: {needs}" if needs else "")
                         + " — if the objective depends on what was NOT observed, call "
                           "report_evidence_gap instead of filling the gap from memory "
                           "or plausibility."
                         + ("" if state == "observed_absent" else
                            " ABSENCE DISCIPLINE: this result does NOT establish that the "
                            "property is absent from the target — only 'observed_absent' "
                            "(the channel was adequately examined and the property is not "
                            "there) can support an absence claim; say 'no X was detected', "
                            "never 'there is no X'."))
            from .. import config as config_mod

            # TRANSPORT SAFETY: the whole result — text plus encoded images —
            # must fit under its SHARE of the SDK frame, because parallel tool
            # results ride one frame and die together.
            budget = config_mod.transport_result_budget()
            text = fit_result_text(text, budget - 16384)
            content: list[dict] = [{"type": "text", "text": text}]
            if produced and ws is not None:
                imgs = image_blocks(produced, ws, config_mod.OBSERVATION_MAX_IMAGES,
                                    config_mod.OBSERVATION_MAX_IMAGE_BYTES,
                                    budget_bytes=budget - len(text) - 4096)
                if imgs:
                    content += imgs
                    if tracer is not None:
                        tracer.event("observation.images", cap.name, status="info",
                                     count=len(imgs))
            return {"content": content}

        return handler

    tools = [make(cap) for cap in capabilities]
    return create_sdk_mcp_server(name=server_name, version="1.0.0", tools=tools)
