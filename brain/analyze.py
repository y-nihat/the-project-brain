"""The Analyst: the first step of the loop, with its full output range.

The loop is analyze -> decompose -> requirements -> generate -> execute. Until
now `analyze` had exactly ONE legal output: decompose. The orchestrator called
the planner as its first act, the planner was forbidden to answer in prose, and
an empty roster was rewritten into a research stage — so every input, however
small, bought a workflow. That is an unfinished first step, not a missing chat
feature: a step that can only ever return one verdict is not judging.

This module gives the step the rest of its range. One judgment, two resolutions:

  * satisfied — nothing must be OBSERVED that the system cannot already state.
    The answer is the execution of that verdict.
  * escalate  — something must be observed. It carries NO category: the planner
    re-derives everything from the objective itself. A verdict that told the
    planner what KIND of work this is would be a triage stage feeding a fixed
    pipeline, which is exactly what the architecture forbids.

Alongside the resolution it answers a second question the engine used to guess
by POSITION: does a stated requirement of this turn depend on an artifact only a
previous turn produced? Continuity used to mean "the previous run of this
process", unconditionally — so a turn that changed the subject still inherited
the last deliverable as evidence about its own topic. Now the referent is
resolved, not assumed: POSITION no longer seeds anything, and the previous turn
has no standing it did not earn. (A relevance-picked prior run can still be
seeded by the separate lexical-history path; what changed is that being
previous is no longer a reason.)

Neither judgment ever inspects the SHAPE of the input. There is no greeting
test, no length test, no keyword list. "Hello" and "prove this file is
well-formed" are answered by the same question — what must be observed that I
cannot already state — and differ only in how much comes back unmet.

Safety: bounded resolutions (it judges; engine code executes), and fail-OPEN to
`escalate`. Any error, timeout, or malformed emission means the full loop runs,
so the Analyst can only ever make the system do LESS work than it would have,
never skip work it owed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import config

RESOLUTIONS = ("satisfied", "escalate")

_SYSTEM = (
    "You are the ANALYZE step of a self-organizing agent system. You are the first thing that "
    "sees what the user just said, and your judgment decides whether the rest of the system runs "
    "at all.\n"
    "\n"
    "Answer ONE question: is there anything that must be OBSERVED — in the world, in a file, in a "
    "system, in a source — before this can be answered honestly, that you cannot already state "
    "yourself?\n"
    "\n"
    "- `satisfied`: nothing must be observed. Everything the user asked for is something you can "
    "state from what you already know and from what this conversation already holds. Put the "
    "actual reply in `answer` — that reply IS the work, so write it as the finished thing the "
    "user reads, not as a description of what could be done.\n"
    "- `escalate`: something must be observed. Say nothing about what KIND of task it is, how "
    "hard it looks, or how it should be done — you are not planning it, and the system that "
    "does plan it starts from the user's own words, not from yours.\n"
    "\n"
    "Judge the REQUIREMENT, never the form of the request. Its length, its tone and how it is "
    "phrased decide nothing: a very brief question can require observation, and a long one may "
    "require none. The only test is whether an honest answer depends on something you would "
    "have to go and look at.\n"
    "\n"
    "Escalate when the answer depends on: the current state of anything (now, today, latest, "
    "live); a specific file, system, page, dataset or account; a claim you would have to verify "
    "rather than assert; a computation or transformation someone must actually run; or anything "
    "the user wants MADE rather than described. When you are unsure, escalate — an unnecessary "
    "escalation costs time, while a wrong `satisfied` ships an unchecked claim as an answer.\n"
    "\n"
    "Never answer from memory about something that changes, and never present a guess as a fact. "
    "If part of a request is answerable and part requires observation, that is `escalate`: the "
    "user asked for one answer, not for the half you happen to hold.\n"
    "\n"
    "SECOND JUDGMENT — REFERENTS. You are shown earlier turns of this conversation. For THIS "
    "turn, decide whether one of its stated requirements depends on an artifact a previous turn "
    "produced — the thing it says 'it', 'that', 'the one you made' about, or names outright. If "
    "so, put that turn's run id in `referent_run_id`. If this turn stands on its own, leave it "
    "empty. Being the most recent turn is NOT a reason: a conversation changes subject, and "
    "inheriting an unrelated artifact makes the system treat a stranger's output as evidence "
    "about this question. Resolve the reference, do not assume it.\n"
    "\n"
    "Call `emit_analysis` exactly once."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "resolution": {"type": "string", "enum": list(RESOLUTIONS)},
        "answer": {"type": "string",
                   "description": "for satisfied: the finished reply the user reads"},
        "referent_run_id": {
            "type": "string",
            "description": "run id of the earlier turn this one depends on; empty if none"},
        "reason": {"type": "string", "description": "one short sentence, for the record"},
    },
    "required": ["resolution", "reason"],
}


@dataclass
class Analysis:
    resolution: str = "escalate"
    answer: str = ""
    referent_run_id: Optional[str] = None
    reason: str = ""
    # True when the verdict came from the engine rather than from judgment
    # (disabled, budget, error) — recorded so a silently-never-firing Analyst
    # is visible instead of looking like a system that always escalates.
    fell_open: bool = False

    @property
    def satisfied(self) -> bool:
        return self.resolution == "satisfied" and bool(self.answer.strip())


def coerce(raw: dict, known_runs: Optional[set] = None) -> Analysis:
    """Whatever the model emitted, the engine acts only on a valid analysis."""
    raw = raw if isinstance(raw, dict) else {}
    resolution = str(raw.get("resolution") or "escalate").strip().lower()
    referent = str(raw.get("referent_run_id") or "").strip() or None
    # A referent decides what gets seeded as this turn's evidence, so an
    # invented id must never reach that decision.
    if referent is not None and known_runs is not None and referent not in known_runs:
        referent = None
    a = Analysis(
        resolution=resolution if resolution in RESOLUTIONS else "escalate",
        answer=str(raw.get("answer") or "")[:20000],
        referent_run_id=referent,
        reason=str(raw.get("reason") or "")[:300],
    )
    # `satisfied` with nothing to say is not a resolution.
    if a.resolution == "satisfied" and not a.answer.strip():
        a.resolution = "escalate"
        a.reason = (a.reason + " (no answer emitted; escalated)").strip()
    return a


# How many earlier turns are in view. ONE definition: the analyze step resolves
# referents over this window and the planner block renders the same one, so a
# referent can never be resolved from a turn the planner is not shown.
CONVERSATION_WINDOW = 6


def conversation_block(turns: list[dict], max_turns: int = CONVERSATION_WINDOW) -> str:
    """Earlier turns, as the material for referent resolution.

    Deliberately thin: what was asked, how it ended, what it left behind. The
    Analyst needs enough to resolve a reference, not enough to re-derive the
    work — and a fat block would make the cheap step expensive.
    """
    rows = [t for t in turns if (t.get("origin") or "user") == "user"][-max_turns:]
    if not rows:
        return ""
    out = ["EARLIER TURNS OF THIS CONVERSATION (oldest first):"]
    for t in rows:
        out.append(
            f"- turn {t.get('turn')} · run id {t.get('run_id')} · {t.get('status')}\n"
            f"    asked: {(t.get('objective') or '')[:300]}\n"
            f"    produced: {(t.get('outcome') or '')[:300]}")
    return "\n".join(out)


class Analyst:
    """``judge_fn`` is a test seam: async (prompt) -> raw analysis dict."""

    def __init__(self, *, model: Optional[str] = None, tracer=None, judge_fn=None):
        from .observability import NullTracer

        self.model = model
        self.tracer = tracer or NullTracer()
        self._judge = judge_fn

    async def analyze(self, objective: str, *, turns: Optional[list[dict]] = None,
                      attached: Optional[list[str]] = None) -> Analysis:
        if not config.ANALYST_ENABLED:
            a = Analysis(reason="analysis step disabled", fell_open=True)
            # Recorded, so a step that never fires is distinguishable in the
            # trace from one that fires and always escalates. That distinction
            # is the whole point of fell_open, which was otherwise written and
            # never read outside tests.
            self.tracer.event("analysis.verdict", "analyze", status="info",
                              resolution=a.resolution, reason=a.reason,
                              decided_by="engine")
            return a
        # Files the user attached ARE things that must be observed, and they are
        # a FACT the engine already holds — no judgment needed, and none that
        # could be talked out of it.
        if attached:
            a = Analysis(reason=f"{len(attached)} provided file(s) must be observed")
            self.tracer.event("analysis.verdict", "analyze", status="info",
                              resolution=a.resolution, reason=a.reason, decided_by="engine")
            return a
        turns = turns or []
        known = {t.get("run_id") for t in turns if t.get("run_id")}
        prompt = objective if not turns else (
            f"{conversation_block(turns)}\n\nTHIS TURN:\n{objective}")
        try:
            raw = await (self._judge(prompt) if self._judge else self._llm(prompt))
            analysis = coerce(raw or {}, known_runs=known)
        except Exception as e:  # noqa: BLE001 — analysis must never break a turn
            self.tracer.event("analysis.error", "analyze", status="failed",
                              error=f"{type(e).__name__}: {str(e)[:200]}")
            return Analysis(reason=f"fail-open: {type(e).__name__}", fell_open=True)
        self.tracer.event("analysis.verdict", "analyze", status="info",
                          resolution=analysis.resolution, reason=analysis.reason,
                          referent=analysis.referent_run_id or "", decided_by="analyst")
        return analysis

    async def _llm(self, prompt: str) -> dict:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        from .factory import locked_session
        from .observability import trace_query

        captured: dict = {}

        @tool("emit_analysis", "Emit your analysis as structured data.", _SCHEMA)
        async def emit_analysis(args):
            captured.update(args or {})
            return {"content": [{"type": "text", "text": "analysis recorded"}]}

        server = create_sdk_mcp_server(name="analyze", version="1.0.0", tools=[emit_analysis])
        options = locked_session(
            system_prompt=_SYSTEM,
            mcp_servers={"analyze": server},
            allowed_tools=["mcp__analyze__emit_analysis"],
            model=self.model or config.model_for("analyst"),
            max_turns=3,
        )
        await trace_query(self.tracer, "analyst", prompt, options,
                          stall_timeout=config.ANALYST_STALL_TIMEOUT,
                          total_timeout=config.ANALYST_TOTAL_TIMEOUT)
        return captured


UNCERTIFIED_NOTE = (
    "[uncertified] Answered directly: this turn required nothing to be observed, so no tool "
    "ran, no source was retained, and nothing verified this against one. It is a statement "
    "from knowledge, not a grounded deliverable."
)


def answer_envelope(analysis: Analysis) -> str:
    """What a directly-answered turn ships.

    The engine's whole claim is that a deliverable's load-bearing claims trace
    to something a tool retained. This answer traces to nothing, and it says so
    in the answer itself rather than only in a status word — otherwise the
    cheapest output in the system would wear the same clothes as the most
    thoroughly checked one.

    The note leads. It used to trail, which put it in the same position as the
    classification caveat that was being sheared off by head-truncation: every
    path that reads an answered turn back takes a head slice — the planner's
    referent block (2500 chars), the conversation view (600), the turn-referent
    fallback (2000) — so a long answer showed a reader the claim and dropped
    the disclaimer. A caveat that survives only on short answers is not a
    caveat.
    """
    return f"{UNCERTIFIED_NOTE}\n\n{analysis.answer.strip()}"
