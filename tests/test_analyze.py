"""The ANALYZE step's full output range.

Offline (no SDK/Docker/token) — the Analyst takes a ``judge_fn`` seam, so every
resolution can be driven deterministically.

What is pinned here is the CONTRACT, not any particular judgment: two
resolutions and no third, escalation carrying no category the planner could be
steered by, fail-open in every failure mode, and continuity that follows a
resolved referent rather than a position.
"""
from __future__ import annotations

import asyncio

import pytest

from brain import analyze
from brain.analyze import Analyst, coerce


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# The vocabulary is bounded
# --------------------------------------------------------------------------- #
def test_only_two_resolutions_exist_and_anything_else_escalates():
    assert analyze.RESOLUTIONS == ("satisfied", "escalate")
    for emitted in ({"resolution": "chat"}, {"resolution": ""}, {}, {"resolution": None},
                    {"resolution": "plan"}, {"resolution": "trivial"}):
        assert coerce(emitted).resolution == "escalate"


def test_satisfied_without_an_answer_is_not_a_resolution():
    """'Nothing must be observed' is only meaningful as the answer itself."""
    a = coerce({"resolution": "satisfied", "answer": "   ", "reason": "easy"})
    assert a.resolution == "escalate" and not a.satisfied


def test_the_emission_carries_no_category_for_the_planner():
    """Escalation must not tell the planner what KIND of work this is — that
    would be a triage step feeding a fixed pipeline. The schema simply has
    nowhere to put one."""
    fields = set(analyze._SCHEMA["properties"])
    assert fields == {"resolution", "answer", "referent_run_id", "reason"}
    for banned in ("category", "kind", "complexity", "effort", "task_type", "stages"):
        assert banned not in fields


def test_the_prompt_never_names_a_kind_of_input():
    """The judgment is about what must be OBSERVED. A greeting list, a length
    rule or a keyword test would be the classifier the architecture forbids."""
    low = analyze._SYSTEM.lower()
    for shape_word in ("greeting", "small talk", "chit", "hello", "casual",
                       "one-word", "word count", "trivial", "simple question"):
        assert shape_word not in low
    # It says the opposite outright, and states the only test there is.
    assert "never the form of the request" in low
    assert "must be observed" in low


# --------------------------------------------------------------------------- #
# Fail-open: analysis can only ever cause MORE work, never less
# --------------------------------------------------------------------------- #
def test_a_broken_judgment_escalates(monkeypatch):
    async def explode(_prompt):
        raise RuntimeError("model unavailable")

    a = _run(Analyst(judge_fn=explode).analyze("anything"))
    assert a.resolution == "escalate" and a.fell_open
    assert "fail-open" in a.reason


def test_a_disabled_step_escalates(monkeypatch):
    monkeypatch.setattr(analyze.config, "ANALYST_ENABLED", False)

    async def never_called(_prompt):  # pragma: no cover
        raise AssertionError("must not be consulted")

    a = _run(Analyst(judge_fn=never_called).analyze("anything"))
    assert a.resolution == "escalate" and a.fell_open


def test_provided_files_escalate_without_asking(monkeypatch):
    """An attached file IS something that must be observed, and that it exists
    is a fact the engine holds — not a judgment that could be talked out of."""
    async def never_called(_prompt):  # pragma: no cover
        raise AssertionError("must not be consulted")

    a = _run(Analyst(judge_fn=never_called).analyze(
        "what is in here", attached=["/tmp/x.pdf"]))
    assert a.resolution == "escalate"
    assert "must be observed" in a.reason


def test_a_satisfied_judgment_answers():
    async def judge(_prompt):
        return {"resolution": "satisfied", "answer": "A mutex allows one holder.",
                "reason": "definitional"}

    a = _run(Analyst(judge_fn=judge).analyze("what is a mutex"))
    assert a.satisfied and a.answer.startswith("A mutex")


# --------------------------------------------------------------------------- #
# The answer says what it is
# --------------------------------------------------------------------------- #
def test_a_direct_answer_ships_visibly_uncertified():
    a = coerce({"resolution": "satisfied", "answer": "Some claim.", "reason": "r"})
    shipped = analyze.answer_envelope(a)
    assert shipped.startswith("Some claim.")
    assert "[uncertified]" in shipped
    assert "no tool ran" in shipped and "not a grounded deliverable" in shipped


# --------------------------------------------------------------------------- #
# Referents: continuity is resolved, not positional
# --------------------------------------------------------------------------- #
_TURNS = [
    {"run_id": "run-a", "turn": 1, "origin": "user", "status": "succeeded",
     "objective": "design a landing page", "outcome": "made page.html"},
    {"run_id": "run-b", "turn": 2, "origin": "user", "status": "succeeded",
     "objective": "summarise this quarter's numbers", "outcome": "a summary"},
]


def test_a_turn_that_refers_to_an_earlier_one_resolves_to_it():
    async def judge(prompt):
        assert "run-a" in prompt and "run-b" in prompt      # candidates offered
        return {"resolution": "escalate", "referent_run_id": "run-a",
                "reason": "'it' is the landing page"}

    a = _run(Analyst(judge_fn=judge).analyze("now deploy it", turns=_TURNS))
    assert a.referent_run_id == "run-a"          # NOT run-b, the most recent


def test_a_turn_that_refers_to_nothing_inherits_nothing():
    async def judge(_prompt):
        return {"resolution": "escalate", "referent_run_id": "", "reason": "stands alone"}

    a = _run(Analyst(judge_fn=judge).analyze("what is the capital of Chile", turns=_TURNS))
    assert a.referent_run_id is None


def test_an_invented_referent_is_discarded():
    """The referent decides what gets seeded as this turn's material, so a
    hallucinated id must never reach that decision."""
    async def judge(_prompt):
        return {"resolution": "escalate", "referent_run_id": "run-that-never-was",
                "reason": "made it up"}

    a = _run(Analyst(judge_fn=judge).analyze("continue", turns=_TURNS))
    assert a.referent_run_id is None


def test_self_initiated_runs_are_not_offered_as_referents():
    turns = _TURNS + [{"run_id": "run-c", "turn": 2, "origin": "pursuit",
                       "status": "succeeded", "objective": "brain's own iteration",
                       "outcome": "x"}]
    block = analyze.conversation_block(turns)
    assert "run-a" in block and "run-b" in block
    assert "run-c" not in block


def test_the_conversation_block_stays_thin():
    """It exists to resolve a reference, not to re-derive the work — a fat
    block would make the cheap step expensive."""
    turns = [{"run_id": f"run-{i}", "turn": i, "origin": "user", "status": "succeeded",
              "objective": "x" * 5000, "outcome": "y" * 5000} for i in range(12)]
    block = analyze.conversation_block(turns)
    assert block.count("run id") == 6           # only the recent handful
    assert len(block) < 5000


# --------------------------------------------------------------------------- #
# Seating: what an answered turn does and does not do
# --------------------------------------------------------------------------- #
def test_an_answered_turn_is_recorded_costed_and_numbered(tmp_path, monkeypatch):
    """An untraced reply would make the cheap path invisible to every view that
    exists — and hide the day the step quietly stops firing."""
    from brain import config
    from brain.observability import Tracer
    from tests.test_conversation import _isolated_session

    session = _isolated_session(tmp_path, monkeypatch)
    try:
        run = session.tracer.start_run("what is a mutex")
        summary = session._deliver_answer(
            run, "what is a mutex",
            coerce({"resolution": "satisfied", "answer": "One holder at a time.",
                    "reason": "definitional"}))
        assert summary.status == "answered"
        assert summary.containers == 0 and summary.leaks == 0
        assert "[uncertified]" in summary.answer
        # Recorded as a real turn: it is the next turn's referent.
        assert session._last_run["run_id"] == run.run_id
        # It ships an ANSWER.md and NO DELIVERABLE.md — the certification
        # vocabulary belongs to work that was checked against something.
        out = config.RUNS_DIR / run.run_id / "outputs"
        assert (out / "ANSWER.md").is_file()
        assert not (out / "DELIVERABLE.md").exists()
    finally:
        session.close()


def test_an_answered_turn_takes_a_turn_ordinal(tmp_path, monkeypatch):
    from tests.test_conversation import _isolated_session

    session = _isolated_session(tmp_path, monkeypatch)
    try:
        first = session.tracer.start_run("hello")
        session._deliver_answer(first, "hello", coerce(
            {"resolution": "satisfied", "answer": "Hello.", "reason": "r"}))
        second = session.tracer.start_run("and again")
        assert first.turn == 1 and second.turn == 2
        second.finish("succeeded", outcome="x")
    finally:
        session.close()


def test_a_self_initiated_iteration_is_never_answered_from_memory():
    """Pursuit runs are not turns. The user granted budget for the brain to DO
    something toward a measured goal, and the result is recorded as an attempt
    against it — an answer from memory would log an attempt that observed
    nothing."""
    import inspect

    from brain import session as session_mod

    src = inspect.getsource(session_mod.BrainSession.run_objective)
    analyse_at = src.index("self._analyst().analyze")
    guard_at = src.index("if self._pursuing:")
    assert guard_at < analyse_at
    assert "self-initiated iteration" in src


def test_the_live_stream_says_which_way_the_turn_went():
    from brain.observability.stream import format_event

    answered = format_event({"kind": "analysis.verdict", "name": "analyze",
                             "status": "info", "data": {"resolution": "satisfied"}})
    assert "uncertified" in answered

    escalated = format_event({"kind": "analysis.verdict", "name": "analyze",
                              "status": "info",
                              "data": {"resolution": "escalate", "referent": "run-a"}})
    assert "must be observed" in escalated and "run-a" in escalated


def test_the_classification_reason_is_not_truncated_before_its_caveat():
    """The honesty clause sits at the END of the reason string; a short cut
    delivered the verdict and dropped the caveat it exists to carry."""
    from brain.observability.stream import format_event

    reason = ("the deliverable answers the objective (grounding not required — no "
              "designated source), and the compiled success criteria were NOT "
              "independently verified")
    line = format_event({"kind": "run.classification", "name": "run", "status": "info",
                         "data": {"classification": "succeeded", "reason": reason}})
    assert "NOT" in line and "independently verified" in line
