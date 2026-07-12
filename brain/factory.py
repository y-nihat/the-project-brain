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
                        team_stages: Optional[list[str]] = None,
                        ready_credentials: Optional[list[str]] = None) -> str:
    """Synthesize an agent's persona/instructions from its spec.

    ``team_stages`` (when the session has the team channel) lists completed
    stages the agent may question via ``ask_upstream``. ``ready_credentials``
    names tools whose credentials the engine has already collected."""
    lines = [
        f'You are the "{spec.role}" agent in a self-organizing, sandboxed multi-agent system.',
        f"Your goal: {spec.goal}",
    ]
    if ready_credentials:
        lines.append(
            "The engine has ALREADY collected and configured the credentials for these tools: "
            + ", ".join(ready_credentials)
            + ". They are ready to call — do NOT ask the user for their credentials, do NOT "
            "claim they are missing, and do NOT trust upstream claims that they are; just call "
            "the tool and report its actual result."
        )
    if team_stages is not None:
        from . import config

        lines.append(
            "You work in a team. If something about an earlier stage's output is unclear or "
            "missing, ask that teammate directly with the ask_upstream tool (completed stages: "
            + (", ".join(team_stages) if team_stages else "none yet")
            + f"; budget {config.TEAM_MAX_QUESTIONS} questions). If an item is impossible "
            "(unreachable source, missing input), report it with report_blocker and move on — "
            "never guess or fabricate around an obstacle; severity='fatal' if the objective "
            "itself cannot be met. EVIDENCE GAP: if the objective depends on something your "
            "tools cannot OBSERVE (a required channel is absent; your observation establishes "
            "identity/metadata but not the substance asked about), call report_evidence_gap — "
            "the engine plans a different observation method. NEVER fill such a gap from "
            "memory, prior runs' narration, or plausibility; deliver what you actually "
            "observed with the gap marked. NEED KNOWLEDGE: if you lack the KNOW-HOW to "
            "proceed — an unfamiliar library/API, an error you don't understand, a technique "
            "or design direction you must decide — call request_research to have the "
            "orchestrator investigate it and return a brief, instead of guessing or stalling "
            "(its findings are method knowledge, not source evidence). During long work, call "
            "progress_update at milestones so the user sees what you are doing."
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
        lines.append(
            "PERCEPTION IS YOUR OWN ABILITY: you are natively multimodal. Some tool "
            "results include IMAGES (files a tool produced, shown inline), and the "
            "read_file tool opens any file the run holds — images come back as images. "
            "Actually LOOK at them: to describe frames, screenshots, scans, rendered "
            "pages, or charts, open them with read_file and report what you SEE — never "
            "what you assume, and never by building a separate vision model."
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
    lines.append(
        "EVIDENCE BOUNDARY: tool results carry a [provenance] line stating their origin. "
        "User-provided files are the factual authority; a tool result marked as produced "
        "'from arguments only (NO input file was read)' is NOT content from the user's "
        "document — never fold it into a synthesis of that document. Keep sources distinct: "
        "do not present derived, fixture, or inferred material as if it came from the user's "
        "evidence, and flag anything you could not ground in a real source. ABSENCE vs "
        "FAILED OBSERVATION: a channel that returns nothing proves only that THAT "
        "observation yielded nothing — never state the source lacks the thing (say "
        "'no X was retrievable', not 'there is no X') unless the absence itself was "
        "positively observed."
    )
    lines.append(
        "RENDERED ARTIFACTS: when the deliverable is something an audience will view (HTML, "
        "a document, slides), author the COMPLETE final content yourself and pass it whole to "
        "a file-writing tool. Visible text must be real, finished copy exactly as the audience "
        "should read it — never markdown markup (**, ##), never your design meta-commentary "
        "('animated dashed lines, violet palette'), never truncated fragments. Give it a real "
        "title. Describe your design in YOUR OWN output; keep it out of the artifact's text."
    )
    lines.append(
        "SOURCE ANCHORING: when working from a provided document, anchor on what the SOURCE "
        "actually contains — its real title and its recurring terms. Do NOT import background "
        "associations you happen to know about the subject or its publisher: if a concept "
        "(a name, a framework, a value) does not appear in the extracted source, it is not a "
        "central theme, no matter how strongly you associate it with the topic. Extract what "
        "IS there; separate direct observations from your own inferences."
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
