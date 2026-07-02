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


def _env_value(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    return json.dumps(v)


def build_caps_server(
    capabilities: Iterable[Capability],
    *,
    manager,
    workspace: Optional[str | Path] = None,
    server_name: str = "caps",
):
    """Create an in-process MCP server exposing each capability as a tool."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    ws = Path(workspace) if workspace else None

    def make(cap: Capability):
        @tool(cap.name, cap.description, cap.input_schema)
        async def handler(args):
            args = dict(args)
            file_refs = args.pop("files", None)  # reserved: workspace paths to attach

            env = {cap.env_map[k]: _env_value(v) for k, v in args.items() if k in cap.env_map}

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
                return {
                    "content": [{
                        "type": "text",
                        "text": f"capability '{cap.name}' failed — {reason}",
                    }],
                    "is_error": True,
                }

            produced: list[str] = []
            if result.output_files and ws is not None:
                capdir = ws / cap.name
                capdir.mkdir(parents=True, exist_ok=True)
                for name, data in result.output_files.items():
                    (capdir / name).write_bytes(data)
                    produced.append(f"{cap.name}/{name}")

            text = result.stdout.strip() or "(no stdout)"
            if produced:
                text += ("\n\nProduced files (pass these paths as `files` to a downstream tool): "
                         + json.dumps(produced))
            if result.error_note:
                text += f"\n\n[engine note] {result.error_note}"
            return {"content": [{"type": "text", "text": text}]}

        return handler

    tools = [make(cap) for cap in capabilities]
    return create_sdk_mcp_server(name=server_name, version="1.0.0", tools=tools)
