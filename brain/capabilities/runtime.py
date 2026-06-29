"""Expose registered capabilities to agents as in-process MCP tools.

Each capability becomes ``mcp__caps__<name>``. The tool handler runs the
capability in a fresh, resource-limited sandbox container (egress only if the
capability declared ``network="bridge"``), captures stdout, tears the container
down, and returns the result. The agent never touches Docker or the host.
"""
from __future__ import annotations

import asyncio
from typing import Iterable

from ..models import Capability
from ..sandbox import policy


def build_caps_server(capabilities: Iterable[Capability], *, manager, server_name: str = "caps"):
    """Create an in-process MCP server exposing each capability as a tool."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    def make(cap: Capability):
        @tool(cap.name, cap.description, cap.input_schema)
        async def handler(args):
            env = {cap.env_map[k]: str(v) for k, v in args.items() if k in cap.env_map}
            pol = policy.egress() if cap.network == "bridge" else policy.isolated()
            # docker-py is synchronous; keep the event loop free.
            result = await asyncio.to_thread(
                manager.run_ephemeral,
                cap.image_tag,
                cap.command,
                policy=pol,
                capability=cap.name,
                environment=env,
            )
            if not result.ok:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"capability '{cap.name}' failed "
                                f"(exit={result.exit_code}, timed_out={result.timed_out}): "
                                f"{result.stderr[:800]}"
                            ),
                        }
                    ],
                    "is_error": True,
                }
            return {"content": [{"type": "text", "text": result.stdout.strip() or "(no output)"}]}

        return handler

    tools = [make(cap) for cap in capabilities]
    return create_sdk_mcp_server(name=server_name, version="1.0.0", tools=tools)
