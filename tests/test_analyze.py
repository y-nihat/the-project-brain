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
def test_a_direct_answer_leads_with_its_uncertified_note():
    """The note LEADS. It used to trail, which put it in the same position as
    the classification caveat that was being sheared off: every path that reads
    an answered turn back takes a HEAD slice — the planner's referent block, the
    conversation view, the turn-referent fallback — so a long answer showed the
    claim and dropped the disclaimer."""
    a = coerce({"resolution": "satisfied", "answer": "Some claim.", "reason": "r"})
    shipped = analyze.answer_envelope(a)
    assert shipped.startswith("[uncertified]")
    assert "no tool ran" in shipped and "not a grounded deliverable" in shipped
    assert shipped.rstrip().endswith("Some claim.")

    # It survives every head-truncation the engine applies downstream.
    long_answer = coerce({"resolution": "satisfied", "answer": "x" * 8000, "reason": "r"})
    envelope = analyze.answer_envelope(long_answer)
    for cut in (600, 2000, 2500):
        assert "[uncertified]" in envelope[:cut], cut


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
    exists — and hide the day the step quietly stops firing.

    Every claim in the name is asserted against the RECORD. The old version
    asserted containers==0 and leaks==0, which RunSummary hardcodes in the same
    expression, so they could not fail for any behavioural reason.
    """
    from brain import config, conversation as conv
    from brain.observability.cost import turn_cost_line
    from tests.test_conversation import _isolated_session

    session = _isolated_session(tmp_path, monkeypatch)
    try:
        run = session.tracer.start_run("what is a mutex")
        summary = session._deliver_answer(
            run, "what is a mutex",
            coerce({"resolution": "satisfied", "answer": "One holder at a time.",
                    "reason": "definitional"}))
        assert summary.status == "answered"
        assert "[uncertified]" in summary.answer
        # RECORDED: the run row exists in the trace store with its own status.
        rows = conv.turns(config.DB_PATH, session.conversation_id)
        assert [r["run_id"] for r in rows] == [run.run_id]
        assert rows[0]["status"] == "answered"
        # NUMBERED: it took a turn ordinal like any other turn.
        assert rows[0]["turn"] == 1
        # COSTED: the accounting views can see it at all.
        assert "conversation so far" in turn_cost_line(
            config.DB_PATH, run.run_id, session.conversation_id)
        # It is the next turn's referent.
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


def test_a_self_initiated_iteration_is_never_answered_from_memory(tmp_path, monkeypatch):
    """Pursuit runs are not turns. The user granted budget for the brain to DO
    something toward a measured goal, and the result is recorded as an attempt
    against it — an answer from memory would log an attempt that observed
    nothing.

    Driven through run_objective, because the previous version asserted a
    dataclass default it constructed itself and reached the real guarantee only
    by grepping the method's source for a statement order.
    """
    from brain import session as session_mod
    from tests.test_conversation import _isolated_session

    consulted = {"n": 0}

    async def judge(_prompt):
        consulted["n"] += 1
        return {"resolution": "satisfied", "answer": "from memory", "reason": "r"}

    class _Orch:
        classification = "succeeded"
        aspiration: dict = {}

        def __init__(self, **_kw):
            pass

        async def run(self, *_a, **_kw):
            return "the work was done"

    monkeypatch.setattr(session_mod, "Orchestrator", _Orch)
    session = _isolated_session(tmp_path, monkeypatch)
    try:
        session._analyst = lambda: Analyst(judge_fn=judge)
        # A user turn consults the step and can be answered directly.
        user = _run(session.run_objective("what is a mutex"))
        assert user.status == "answered" and consulted["n"] == 1

        # A self-initiated iteration never reaches it, and never comes back
        # 'answered' however willing the judge is to answer.
        session._pursuing = True
        pursuit = _run(session.run_objective("iterate toward the goal"))
        assert pursuit.status != "answered", pursuit.status
        assert consulted["n"] == 1, "the analyze step was consulted for a pursuit run"
    finally:
        session.close()


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
    """The honesty clause sits at the END of the reason string, so ANY head
    truncation delivers the verdict and drops the caveat. Recorded reasons
    reach 1049 characters — this fixture is deliberately longer than any cut
    the renderer has ever applied."""
    from brain.observability.stream import format_event

    reason = ("the deliverable answers the objective " + "(with a long diagnosis) " * 40
              + "and the compiled success criteria were NOT independently verified")
    assert len(reason) > 1000
    line = format_event({"kind": "run.classification", "name": "run", "status": "info",
                         "data": {"classification": "succeeded", "reason": reason}})
    assert line.endswith("NOT independently verified")


def test_an_answered_turn_masks_secret_shapes_before_writing_to_disk(tmp_path, monkeypatch):
    """Every answer the engine writes is masked; this path is newer than the
    guarantee and was written without it."""
    from brain import config
    from tests.test_conversation import _isolated_session

    # Assembled at run time: a literal of this shape in a tracked file is
    # exactly what the repo's own secret guard exists to refuse.
    canary = "sk-ant-" + "answeredturncanary0123456789"
    session = _isolated_session(tmp_path, monkeypatch)
    try:
        run = session.tracer.start_run("what is my token")
        session._deliver_answer(run, "what is my token", coerce(
            {"resolution": "satisfied",
             "answer": f"The value you pasted was {canary}.", "reason": "r"}))
        on_disk = (config.RUNS_DIR / run.run_id / "outputs" / "ANSWER.md").read_text()
        assert canary not in on_disk
        assert "The value you pasted was" in on_disk      # only the secret is masked
    finally:
        session.close()


def test_an_answered_turn_builds_no_orchestration_at_all(tmp_path, monkeypatch):
    """The property the whole change exists for, and the one nothing asserted.

    Every other test calls ``_deliver_answer`` directly, so none of them drives
    ``run_objective`` to the point where a satisfied verdict short-circuits —
    and the containers/leaks numbers they looked at are literals hardcoded in
    the RunSummary. A regression that ran the full pipeline and then discarded
    it would have been invisible.
    """
    from brain import session as session_mod
    from tests.test_conversation import _isolated_session

    built = {"orchestrators": 0, "sandboxes": 0, "plans": 0}

    class _Orch:
        classification = "succeeded"
        aspiration: dict = {}

        def __init__(self, **_kw):
            built["orchestrators"] += 1

        async def run(self, *_a, **_kw):  # pragma: no cover — must not be reached
            raise AssertionError("the full loop ran for an answered turn")

    def _mgr(*_a, **_kw):
        built["sandboxes"] += 1
        raise AssertionError("a sandbox manager was built for an answered turn")

    class _Planner:
        def __init__(self, *_a, **_kw):
            built["plans"] += 1

    monkeypatch.setattr(session_mod, "Orchestrator", _Orch)
    monkeypatch.setattr(session_mod, "SandboxManager", _mgr)
    monkeypatch.setattr(session_mod, "Planner", _Planner)

    async def judge(_prompt):
        return {"resolution": "satisfied", "answer": "One holder at a time.",
                "reason": "definitional"}

    session = _isolated_session(tmp_path, monkeypatch)
    try:
        session._analyst = lambda: Analyst(judge_fn=judge)
        summary = _run(session.run_objective("what is a mutex"))
    finally:
        session.close()

    assert summary.status == "answered"
    assert built == {"orchestrators": 0, "sandboxes": 0, "plans": 0}, built


def test_an_answered_turn_never_becomes_target_tier_evidence(tmp_path, monkeypatch):
    """The hollow certificate this closes: an `answered` run's ANSWER.md is
    model prose that observed nothing BY CONSTRUCTION and says so in its own
    text. Seeding it for a later turn stamped it tier=target, coverage=full,
    state=observed_present — the strongest claim the evidence plane makes — and
    one such entry satisfies target_lineage(), suppressing the 'narration was
    approved, not evidence' void the ledger exists to raise.

    The file is still placed in the workspace so an agent can READ what was
    said. It simply is not evidence.
    """
    import asyncio

    from brain import config, evidence
    from brain.memory.blackboard import Blackboard
    from brain.models import StageSpec
    from brain.observability import NullTracer
    from brain.orchestrator import Orchestrator
    from brain.planner import StubPlanner

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    prior = tmp_path / "runs" / "prev-answered"
    (prior / "outputs").mkdir(parents=True)
    (prior / "workspace").mkdir()
    # Exactly what _deliver_answer writes: the note, then the answer.
    (prior / "outputs" / "ANSWER.md").write_text(
        analyze.answer_envelope(coerce({"resolution": "satisfied",
                                        "answer": "The widget is blue.",
                                        "reason": "definitional"})))

    async def runner(_t, _name, _prompt, _options):
        return "done"

    bb = Blackboard(tmp_path / "b.db", "later")
    orch = Orchestrator(session="later", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None,
                        planner=StubPlanner([StageSpec(id="w", pattern="single",
                                                       role="W", goal="work")]),
                        resolver=None, session_runner=runner, tracer=NullTracer())
    try:
        asyncio.run(orch.run("now act on it", seed_run="prev-answered",
                             referent_resolved=True))
    finally:
        bb.close()

    run_dir = config.RUNS_DIR / "later"
    # Readable...
    seeded = run_dir / "workspace" / "prior" / "prev-answered" / "outputs" / "ANSWER.md"
    assert seeded.is_file(), "the prior answer must still be readable"
    # ...but never DESIGNATED. It may appear as an ordinary retained file in
    # the workspace; what it must never be is target tier, which is the claim
    # that satisfies target_lineage and certifies a run.
    ledger = evidence.ledger(run_dir, [], [])
    seeded_entries = [e for e in ledger if "prev-answered" in str(e.file)]
    assert seeded_entries, "the seeded file should still be visible to the ledger"
    assert all(e.tier != "target" for e in seeded_entries), seeded_entries
    assert not evidence.target_lineage(ledger), evidence.target_lineage(ledger)
