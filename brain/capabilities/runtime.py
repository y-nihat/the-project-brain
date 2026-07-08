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
import json
from pathlib import Path
from typing import Iterable, Optional

from ..models import Capability
from ..sandbox import policy
from . import health as health_mod


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

            # Observe what actually came back (ok / empty / error) — the silent
            # failure mode is exit 0 with no data.
            rclass, count = health_mod.classify_result(result.stdout)
            if produced and rclass == health_mod.EMPTY:
                rclass, count = health_mod.OK, len(produced)  # producing files is a real result
            _observe(monitor, tracer, cap.name, rclass, count)

            text = result.stdout.strip() or "(no stdout)"
            if produced:
                text += ("\n\nProduced files (pass these paths as `files` to a downstream tool): "
                         + json.dumps(produced))
            if result.error_note:
                text += f"\n\n[engine note] {result.error_note}"
            if rclass == health_mod.EMPTY:
                text += ("\n\n[engine note] this returned NO results. If this keeps happening the "
                         "tool may be degraded — vary your query or stop and report the limitation.")
            return {"content": [{"type": "text", "text": text}]}

        return handler

    tools = [make(cap) for cap in capabilities]
    return create_sdk_mcp_server(name=server_name, version="1.0.0", tools=tools)
