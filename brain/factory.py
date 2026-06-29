"""Agent-session factory: locked-down ClaudeAgentOptions per role.

Every agent session is hard-confined:
  * ``tools=[]``            — no built-in/host tools in context (no Bash, Write,
                              Read, Edit, and no ``Agent`` tool => no recursion).
  * ``permission_mode="dontAsk"`` — anything not pre-approved is denied outright,
                              with no prompt that could misfire.
  * ``allowed_tools=[...]`` — exactly the MCP capability/structured tools we hand it.
  * ``setting_sources=[]``  — no host permission rules (~/.claude, project) leak in.

The result: an agent's only reachable action is calling one of its provided MCP
tools, which the orchestrator runs inside a sandbox container.
"""
from __future__ import annotations

from typing import Iterable, Optional

RESEARCHER_PROMPT = (
    "You are a research agent inside a fully isolated, sandboxed system. You can ONLY act "
    "by calling the capability tools provided to you (their names start with `mcp__caps__`). "
    "You have no shell, no filesystem, and no other tools. Use the web_search capability to "
    "gather live information relevant to the user's objective, then write a concise answer and "
    "cite the source URLs you relied on. If a capability returns an error, report it plainly — "
    "never fabricate results."
)


def locked_session(
    *,
    system_prompt: str,
    mcp_servers: dict,
    allowed_tools: Iterable[str],
    model: Optional[str] = None,
    max_turns: int = 8,
):
    """Build a hard-confined ClaudeAgentOptions for an agent session."""
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers=mcp_servers,
        tools=[],                       # remove all built-in/host tools from context
        allowed_tools=list(allowed_tools),
        permission_mode="dontAsk",      # deny anything not allow-listed, no prompt
        setting_sources=[],             # ignore host/project permission settings
        model=model,
        max_turns=max_turns,
    )
