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
# Persistence judgment at a failure/recovery boundary (Component 2).
PERSIST_ACTIONS = ("keep_trying", "stop_and_ask", "stop")
# Continuation judgment inside the tool-authoring loop.
CONTINUE_ACTIONS = ("continue", "stop")

_PERSIST_SYSTEM = (
    "You are the executive judgment of an autonomous run at a FAILURE/RECOVERY boundary: a "
    "stage's method just failed and the run can try another approach, stop and ask the user, "
    "or stop. Decide like a capable human problem-solver whose goal is to COMPLETE THE TASK "
    "CORRECTLY — not quickly:\n"
    "- keep_trying: a materially DIFFERENT, admissible approach remains AND the attempts so "
    "far are still making progress — each is learning something or closing the gap to the "
    "stage's expected yield. Prefer this while progress continues; do not give up early just "
    "because a couple of attempts failed.\n"
    "- stop_and_ask: you have genuinely explored the viable approaches and are stuck, OR "
    "progress has PLATEAUED (repeated attempts hit the same wall with no new information), OR "
    "finishing needs something only the USER can provide — a file, a credential, access, or a "
    "decision. In `needed`, state EXACTLY what you need from the user, in one or two sentences.\n"
    "- stop: there is no admissible path at all and no user input would change that; terminate "
    "honestly.\n"
    "Judge PROGRESS from the attempt history and the expectation-vs-observation gap, NOT from a "
    "fixed attempt count. A hard safety ceiling bounds the run regardless, so your job is to "
    "stop EARLY when more attempts are futile, and to keep going when they are still "
    "productive.\n"
    "INFORMATION EPOCHS: attempt-history lines may carry [epoch N] markers and 'new "
    "information' boundaries (a credential became available, the first runtime evidence "
    "arrived, research was delivered). Repetition counts WITHIN an epoch only — attempts made "
    "BEFORE new information do not discredit approaches that information enables; a person who "
    "just learned what was missing tries several methods with that new knowledge before "
    "concluding exhaustion. Never claim something must be provided (a credential, a file) when "
    "the digest's structured state says the run already HOLDS it. Be decisive; call "
    "emit_decision exactly once."
)

_PERSIST_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(PERSIST_ACTIONS)},
        "needed": {"type": "string",
                   "description": "for stop_and_ask: exactly what you need from the user"},
        "reason": {"type": "string", "description": "one short sentence"},
    },
    "required": ["action", "reason"],
}

_CONT_SYSTEM = (
    "You are the executive judgment INSIDE an automated tool-authoring loop: a developer "
    "agent has been attempting to build one tool and its latest attempt failed. Decide "
    "whether ANOTHER attempt is justified, like a capable engineer supervising a colleague:\n"
    "- continue: a materially different next approach exists, or new information (a "
    "credential now available, fresh runtime evidence, research findings) has not yet been "
    "seriously exercised. In `next_approach`, state concretely what the next attempt should "
    "do DIFFERENTLY — it is handed to the developer as guidance.\n"
    "- stop: the approaches admissible under the CURRENT information state are exhausted — "
    "more attempts would repeat, not learn.\n"
    "The history is grouped into INFORMATION EPOCHS: an epoch boundary marks genuinely new "
    "information entering the loop. Repetition counts WITHIN an epoch only — attempts made "
    "before new information do not discredit approaches that information enables; a person "
    "who just learned what was missing tries several methods with it before giving up. "
    "Distinguish failure kinds: a mechanical slip (a missing declaration, a truncated "
    "submission) beside real implementation progress is a reason to CONTINUE with the fix "
    "named, not evidence of a plateau. Never treat a credential the run already HOLDS as "
    "missing. A hard attempt ceiling bounds the loop regardless, so stop only when "
    "continuing is genuinely futile. Be decisive; call emit_decision exactly once."
)

_CONT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(CONTINUE_ACTIONS)},
        "next_approach": {"type": "string",
                          "description": "for continue: what the next attempt should do differently"},
        "reason": {"type": "string", "description": "one short sentence"},
    },
    "required": ["action", "reason"],
}

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
        # Failure-boundary judgments get their OWN budget: routine stage
        # consultations must never starve the decision that governs whether the
        # run keeps exploring or asks for help.
        self.persist_calls = 0
        # Authoring-loop continuation judgments: separate again, so a long
        # authoring struggle cannot starve the stage-level persistence decision.
        self.cont_calls = 0

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

    async def persistence(self, digest: str) -> Decision:
        """Human-like persistence judgment at a failure/recovery boundary:
        keep_trying (a materially different, admissible approach remains AND
        attempts are still making progress), stop_and_ask (approaches exhausted
        or progress plateaued, or something only the USER can provide is needed —
        `message` states exactly what), or stop (no admissible path). Fail-OPEN
        to keep_trying — hard ceilings still bound the run — so supervision can
        only make the system stop EARLIER, never remove the safety rail."""
        if not config.SUPERVISOR_ENABLED:
            return Decision(action="keep_trying", reason="supervision disabled")
        if self.persist_calls >= config.PERSISTENCE_MAX_CALLS:
            return Decision(action="keep_trying",
                            reason="persistence-decision budget exhausted")
        self.persist_calls += 1
        try:
            raw = await (self._decide("persistence", digest) if self._decide
                         else self._llm_persistence(digest))
            raw = raw if isinstance(raw, dict) else {}
            action = str(raw.get("action") or "keep_trying").strip().lower()
            if action not in PERSIST_ACTIONS:
                action = "keep_trying"
            d = Decision(action=action, message=str(raw.get("needed") or "")[:2000],
                         reason=str(raw.get("reason") or "")[:300])
        except Exception as e:  # noqa: BLE001 — persistence must never break a run
            self.tracer.event("supervisor.error", "persistence", status="failed",
                              error=str(e)[:200])
            return Decision(action="keep_trying", reason=f"fail-open: {type(e).__name__}")
        self.tracer.event("supervisor.persistence", "recovery", status="info",
                          action=d.action, reason=d.reason)
        return d

    async def continuation(self, digest: str) -> Decision:
        """Continuation judgment INSIDE the tool-authoring loop: continue (with a
        materially different or newly-enabled `next_approach`, handed to the
        developer as guidance) or stop (the current information state is
        exhausted). Fail-OPEN to continue — the static attempt ceiling still
        bounds the loop — so this judgment can only stop the loop EARLIER,
        never remove the safety rail."""
        if not config.SUPERVISOR_ENABLED:
            return Decision(action="continue", reason="supervision disabled")
        if self.cont_calls >= config.CONTINUATION_MAX_CALLS:
            return Decision(action="continue",
                            reason="continuation-decision budget exhausted")
        self.cont_calls += 1
        try:
            raw = await (self._decide("continuation", digest) if self._decide
                         else self._llm_continuation(digest))
            raw = raw if isinstance(raw, dict) else {}
            action = str(raw.get("action") or "continue").strip().lower()
            if action not in CONTINUE_ACTIONS:
                action = "continue"
            d = Decision(action=action,
                         message=str(raw.get("next_approach") or "")[:2000],
                         reason=str(raw.get("reason") or "")[:300])
        except Exception as e:  # noqa: BLE001 — continuation must never break a run
            self.tracer.event("supervisor.error", "continuation", status="failed",
                              error=str(e)[:200])
            return Decision(action="continue", reason=f"fail-open: {type(e).__name__}")
        self.tracer.event("supervisor.continuation", "authoring", status="info",
                          action=d.action, reason=d.reason,
                          next_approach=d.message[:200])
        return d

    async def _llm(self, moment: str, digest: str) -> dict:
        return await self._emit(_SYSTEM, _SCHEMA, f"DECISION MOMENT: {moment}\n\n{digest}",
                                "supervisor")

    async def _llm_persistence(self, digest: str) -> dict:
        # Persistence is a strategy-heavy judgment (compare approaches, judge
        # progress, decide whether to ask the user) — it gets the STRONG tier,
        # unlike routine supervision moments which stay fast.
        return await self._emit(_PERSIST_SYSTEM, _PERSIST_SCHEMA, digest,
                                "supervisor.persistence",
                                model=config.model_for("persistence"))

    async def _llm_continuation(self, digest: str) -> dict:
        # Same strategy tier as persistence: comparing approach categories and
        # judging whether new information has been exercised is strategy.
        return await self._emit(_CONT_SYSTEM, _CONT_SCHEMA, digest,
                                "supervisor.continuation",
                                model=config.model_for("continuation"))

    async def _emit(self, system_prompt: str, schema: dict, prompt: str, label: str,
                    model=None) -> dict:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        from .factory import locked_session
        from .observability import trace_query

        captured: dict = {}

        @tool("emit_decision", "Emit your decision as structured data.", schema)
        async def emit_decision(args):
            captured.update(args or {})
            return {"content": [{"type": "text", "text": "decision recorded"}]}

        server = create_sdk_mcp_server(name="sup", version="1.0.0", tools=[emit_decision])
        options = locked_session(
            system_prompt=system_prompt,
            mcp_servers={"sup": server},
            allowed_tools=["mcp__sup__emit_decision"],
            model=self.model or model or config.model_for("supervisor"),
            max_turns=3,
        )
        await trace_query(self.tracer, label, prompt, options,
                          stall_timeout=90, total_timeout=180)
        return captured
