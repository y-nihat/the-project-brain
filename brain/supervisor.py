"""Executive function: observe the run's state, judge like a person would.

The orchestrator's reflexes (budgets, watchdogs, retries) are its spinal cord;
the Supervisor is the judgment above them. It is consulted at decision moments —
before any user interaction, after each stage — with a compact digest of the
run's state, and answers from a BOUNDED action vocabulary:

  * proceed     — everything is in order (the default)
  * inform_user — show the user something FIRST (message = the exact text)
  * redo_stage  — the completed stage clearly fails the objective (guidance = fix)
  * abort       — continuing would be harmful or pointless

Safety properties: bounded actions (it advises; engine code executes), a hard
per-run call budget, and fail-open — any error, timeout, or malformed answer
degrades to `proceed` so supervision can never break a run.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import config

ACTIONS = ("proceed", "inform_user", "redo_stage", "abort")

_SYSTEM = (
    "You are the executive supervisor of an autonomous multi-agent run — the judgment a "
    "sensible human operator would apply. You are shown the run's state at a decision "
    "moment. Choose ONE action:\n"
    "- proceed: everything is in order. This is the default; prefer it. Do not intervene "
    "for style or minor imperfections.\n"
    "- inform_user: something should be shown or explained to the user RIGHT NOW, before "
    "what happens next. `message` is the exact text to display. Use this when the user is "
    "about to be asked for something without the instructions or context they need to "
    "answer (write those instructions yourself if you know them), or when the user "
    "explicitly asked to be told something that has not been shown yet.\n"
    "- redo_stage: the just-completed stage's output clearly violates the objective's "
    "EXPLICIT requirements (a stated count, a required deliverable, a demanded format). "
    "`guidance` is a concrete instruction for the fix. Not for taste.\n"
    "- abort: continuing is clearly harmful or pointless.\n"
    "inform_user messages are NONBLOCKING NOTICES — the run continues immediately; never "
    "phrase one as a question or a set of options awaiting the user's choice.\n"
    "STATE DISCIPLINE: the digest's structured facts (REVIEW VERDICTS, observed files) "
    "are authoritative over any stage's own prose — never assert 'verified', 'approved', "
    "'grounded', or 'criteria met' unless the structured state says so. A failed or "
    "empty observation is NOT evidence of absence in the source: 'the channel returned "
    "nothing' never becomes 'the source lacks it'.\n"
    "Be decisive, be brief, and call `emit_decision` exactly once."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "message": {"type": "string", "description": "text to show the user (inform_user)"},
        "guidance": {"type": "string", "description": "fix instruction (redo_stage)"},
        "reason": {"type": "string", "description": "one short sentence"},
    },
    "required": ["action", "reason"],
}


@dataclass
class Decision:
    action: str = "proceed"
    message: str = ""
    guidance: str = ""
    reason: str = ""


def coerce_decision(raw: dict, allowed: tuple = ACTIONS) -> Decision:
    """Whatever the model emitted, the engine executes only a valid decision."""
    raw = raw if isinstance(raw, dict) else {}
    action = str(raw.get("action") or "proceed").strip().lower()
    d = Decision(
        action=action if action in allowed else "proceed",
        message=str(raw.get("message") or "")[:4000],
        guidance=str(raw.get("guidance") or "")[:2000],
        reason=str(raw.get("reason") or "")[:300],
    )
    if d.action == "inform_user" and not d.message.strip():
        d.action = "proceed"
    if d.action == "redo_stage" and not d.guidance.strip():
        d.action = "proceed"
    return d


class Supervisor:
    """``decide_fn`` is a test seam: async (moment, digest) -> raw decision dict."""

    def __init__(self, *, model: Optional[str] = None, tracer=None,
                 decide_fn=None, max_calls: Optional[int] = None):
        from .observability import NullTracer

        self.model = model
        self.tracer = tracer or NullTracer()
        self._decide = decide_fn
        self.max_calls = max_calls if max_calls is not None else config.SUPERVISOR_MAX_CALLS
        self.calls = 0

    async def consult(self, moment: str, digest: str, allowed: tuple = ACTIONS) -> Decision:
        if not config.SUPERVISOR_ENABLED:
            return Decision(reason="supervision disabled")
        if self.calls >= self.max_calls:
            return Decision(reason=f"supervisor call budget ({self.max_calls}) exhausted")
        self.calls += 1
        try:
            raw = await (self._decide(moment, digest) if self._decide
                         else self._llm(moment, digest))
            decision = coerce_decision(raw or {}, allowed)
        except Exception as e:  # noqa: BLE001 — supervision must never break a run
            self.tracer.event("supervisor.error", moment, status="failed", error=str(e)[:300])
            return Decision(reason=f"fail-open: {type(e).__name__}")
        self.tracer.event("supervisor.consult", moment, status="info",
                          action=decision.action, reason=decision.reason)
        return decision

    async def _llm(self, moment: str, digest: str) -> dict:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        from .factory import locked_session
        from .observability import trace_query

        captured: dict = {}

        @tool("emit_decision", "Emit your decision as structured data.", _SCHEMA)
        async def emit_decision(args):
            captured.update(args or {})
            return {"content": [{"type": "text", "text": "decision recorded"}]}

        server = create_sdk_mcp_server(name="sup", version="1.0.0", tools=[emit_decision])
        options = locked_session(
            system_prompt=_SYSTEM,
            mcp_servers={"sup": server},
            allowed_tools=["mcp__sup__emit_decision"],
            model=self.model or config.model_for("supervisor"),
            max_turns=3,
        )
        await trace_query(self.tracer, "supervisor", f"DECISION MOMENT: {moment}\n\n{digest}",
                          options, stall_timeout=90, total_timeout=180)
        return captured
