"""Agent-session factory: dynamic agents, hard-confined.

Two parts:
  * ``locked_session`` — builds a ClaudeAgentOptions that is hard-confined:
    ``tools=[]`` (no host/built-in tools, no ``Agent`` => no recursion),
    ``permission_mode="dontAsk"`` (anything not allow-listed is denied),
    ``setting_sources=[]`` (no host permission rules leak in), and only the MCP
    capability tools we hand it.
  * ``agent_system_prompt`` / ``agent_user_prompt`` — synthesize the persona and
    task prompt for an agent from its :class:`~brain.models.AgentSpec`, so agent
    instructions are derived per task rather than hardcoded.
"""
from __future__ import annotations

from typing import Iterable, Optional

from .models import AgentSpec


def locked_session(
    *,
    system_prompt: str,
    mcp_servers: dict,
    allowed_tools: Iterable[str],
    model: Optional[str] = None,
    max_turns: int = 10,
):
    """Build a hard-confined ClaudeAgentOptions for an agent session."""
    from claude_agent_sdk import ClaudeAgentOptions

    kwargs = dict(
        system_prompt=system_prompt,
        mcp_servers=mcp_servers,
        tools=[],                       # remove all built-in/host tools from context
        allowed_tools=list(allowed_tools),
        permission_mode="dontAsk",      # deny anything not allow-listed, no prompt
        setting_sources=[],             # ignore host/project permission settings
        model=model,
        max_turns=max_turns,
    )
    try:
        # Stream partial messages so the session watchdog sees token generation
        # as liveness — a long healthy synthesis must not look like a stall.
        return ClaudeAgentOptions(**kwargs, include_partial_messages=True)
    except TypeError:  # older SDK without the field
        return ClaudeAgentOptions(**kwargs)


def agent_system_prompt(spec: AgentSpec, allowed_tools: list[str],
                        team_stages: Optional[list[str]] = None) -> str:
    """Synthesize an agent's persona/instructions from its spec.

    ``team_stages`` (when the session has the team channel) lists completed
    stages the agent may question via ``ask_upstream``."""
    lines = [
        f'You are the "{spec.role}" agent in a self-organizing, sandboxed multi-agent system.',
        f"Your goal: {spec.goal}",
    ]
    if team_stages is not None:
        from . import config

        lines.append(
            "You work in a team. If something about an earlier stage's output is unclear or "
            "missing, ask that teammate directly with the ask_upstream tool (completed stages: "
            + (", ".join(team_stages) if team_stages else "none yet")
            + f"; budget {config.TEAM_MAX_QUESTIONS} questions). If an item is impossible "
            "(unreachable source, missing input), report it with report_blocker and move on — "
            "never guess or fabricate around an obstacle."
        )
    if allowed_tools:
        from . import config

        lines.append(
            "You can act ONLY by calling these capability tools: "
            + ", ".join(allowed_tools)
            + ". You have no shell, no filesystem, and no other tools."
        )
        lines.append(
            f"Work efficiently: you have a budget of ~{config.CAPABILITY_CALL_BUDGET} calls PER tool. "
            "Do not hammer a tool with near-duplicate calls — vary your inputs, gather enough, then "
            "synthesize and conclude. If a tool keeps returning no results, stop calling it and say so."
        )
    else:
        lines.append(
            "You have no tools. Produce your result using only the objective and the "
            "context handed to you by upstream agents."
        )
    lines.append(
        "Use only real data. If a tool returns an error, report it plainly and never "
        "fabricate. When finished, output your result clearly and concisely; cite source "
        "URLs if you used web search."
    )
    return "\n".join(lines)


def agent_user_prompt(
    objective: str,
    spec: AgentSpec,
    upstream: dict[str, str],
    history: str | None = None,
    task_context: str | None = None,
) -> str:
    """Build the per-agent task prompt, injecting upstream outputs and, when
    present, the brain's relevant prior-run history.

    ``task_context`` carries stage-pattern material the orchestrator computes at
    run time — a map worker's shard, a reducer's notes, a reviewer's evidence,
    or critique a draft must address."""
    parts = [
        f"Overall objective: {objective}",
        f"Your task ({spec.role}): {spec.goal}",
    ]
    if history:
        parts.append(
            "\nPrior work by this system relevant to the objective (files listed there may "
            "already be seeded under `prior/` in the current workspace):\n" + history
        )
    if upstream:
        parts.append("\nContext from upstream agents:")
        for dep, out in upstream.items():
            parts.append(f"\n[{dep}]:\n{out}")
    if task_context:
        parts.append("\n" + task_context)
    parts.append("\nProduce your result now.")
    return "\n".join(parts)
