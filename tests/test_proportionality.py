"""Post-plan effort follows the plan.

Offline. The defect this covers is the structural twin of the analyze step's:
``deliverable_stage_ids`` can never return empty, so "this plan produces no
artifact of record" was inexpressible, and everything keyed to that question ran
for every plan — the fenced-payload contract with its counting and self-render
rules, a renderer bolted onto the producing stage, format measurement, a
DELIVERABLE.md. A turn with no artifact paid all of it.

What is pinned here is that the verdict is read off the PLAN and nothing else.
"""
from __future__ import annotations

from brain import workflow
from brain.models import StageSpec


def _stage(sid: str, **kw) -> StageSpec:
    return StageSpec(id=sid, pattern=kw.pop("pattern", "single"),
                     role=kw.pop("role", sid), goal=kw.pop("goal", ""), **kw)


# --------------------------------------------------------------------------- #
# The verdict can now be 'no'
# --------------------------------------------------------------------------- #
def test_a_plan_with_no_artifact_signal_produces_none():
    stages = [_stage("look"), _stage("say", depends_on=["look"])]
    assert workflow.produces_artifact_of_record(stages) is False
    # ...while the routing question still always has an answer, as it must.
    assert workflow.deliverable_stage_ids(stages)


def test_a_marked_stage_settles_it():
    stages = [_stage("make", deliverable=True)]
    assert workflow.produces_artifact_of_record(stages) is True


def test_a_review_stage_implies_a_draft_to_review():
    stages = [_stage("draft"), _stage("check", pattern="review", depends_on=["draft"])]
    assert workflow.produces_artifact_of_record(stages) is True


def test_a_capability_that_emits_an_artifact_settles_it():
    stages = [_stage("make", capabilities=["render_deck"])]
    assert workflow.produces_artifact_of_record(
        stages, emits_by_cap={"render_deck": "artifact"}) is True
    # An observing or deriving tool says nothing about an artifact of record.
    for role in ("observation", "derived", "validation", ""):
        assert workflow.produces_artifact_of_record(
            stages, emits_by_cap={"render_deck": role}) is False


def test_a_countable_format_criterion_settles_it():
    stages = [_stage("write")]
    assert workflow.produces_artifact_of_record(
        stages, criteria=["exactly 3 sections"]) is True
    # A criterion with no countable bound is not a signal.
    assert workflow.produces_artifact_of_record(
        stages, criteria=["the tone should be friendly"]) is False


def test_declared_knowledge_needs_settle_it():
    """They are declared exactly when a deliverable's FORM will be judged."""
    assert workflow.produces_artifact_of_record(
        [_stage("make")], knowledge_declared=True) is True


def test_the_verdict_never_looks_at_the_objective():
    """It is a property of the PLAN. There is nowhere to pass an input, which
    is what keeps this from becoming a request classifier."""
    import inspect

    sig = inspect.signature(workflow.produces_artifact_of_record)
    assert set(sig.parameters) == {"stages", "criteria", "emits_by_cap",
                                   "knowledge_declared"}


# --------------------------------------------------------------------------- #
# The machinery keyed to it is actually conditioned
# --------------------------------------------------------------------------- #
def _run_plan(tmp_path, monkeypatch, session, stages, criteria=None):
    """Drive a real Orchestrator over a plan and return (prompts, events)."""
    import asyncio

    from brain import config
    from brain.memory.blackboard import Blackboard
    from brain.observability import NullTracer
    from brain.orchestrator import Orchestrator
    from brain.planner import StubPlanner

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    prompts: dict[str, str] = {}
    events: list[tuple] = []

    class Recording(NullTracer):
        def event(self, kind, subject, status="info", **data):
            events.append((kind, data))

    async def runner(_t, name, prompt, options):
        prompts[name] = prompt
        if name.startswith("check.r"):
            return "VERDICT: approved\nok"
        return "```deliverable\nthe work\n```"

    bb = Blackboard(tmp_path / f"{session}.db", session)
    orch = Orchestrator(session=session, manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None,
                        planner=StubPlanner(stages, success_criteria=criteria or []),
                        resolver=None, session_runner=runner, tracer=Recording())
    try:
        asyncio.run(orch.run("do the thing"))
    finally:
        bb.close()
    return prompts, events


def test_a_plan_with_no_artifact_pays_none_of_the_deliverable_machinery(
        tmp_path, monkeypatch):
    """The CONSEQUENCE of the verdict, which nothing asserted — the decision
    had six tests and its effect had none. A source lint stood in for this and
    was blind to any gate written with a differently-named local."""
    from brain import config

    prompts, events = _run_plan(
        tmp_path, monkeypatch, "noart",
        [StageSpec(id="say", pattern="single", role="S", goal="answer")])

    verdict = next(d for k, d in events if k == "plan.artifact_of_record")
    assert verdict["produces"] is False
    # No fenced-payload contract in the producing stage's prompt...
    assert "DELIVERABLE PAYLOAD" not in prompts["say"]
    assert "SELF-CHECK BEFORE HANDOFF" not in prompts["say"]
    # ...no DELIVERABLE.md asserting an artifact that does not exist...
    assert not (config.RUNS_DIR / "noart" / "outputs" / "DELIVERABLE.md").exists()
    # ...and no format measurement.
    assert not any(k == "criteria.measured" for k, _d in events)


def test_a_plan_that_does_produce_an_artifact_still_gets_everything(
        tmp_path, monkeypatch):
    """The mirror case, so the gate cannot pass by simply never firing."""
    from brain import config

    prompts, events = _run_plan(
        tmp_path, monkeypatch, "art",
        [StageSpec(id="make", pattern="single", role="M", goal="produce",
                   deliverable=True)],
        criteria=["exactly 1 section"])

    verdict = next(d for k, d in events if k == "plan.artifact_of_record")
    assert verdict["produces"] is True
    assert "DELIVERABLE PAYLOAD" in prompts["make"]
    assert (config.RUNS_DIR / "art" / "outputs" / "DELIVERABLE.md").is_file()
    assert any(k == "criteria.measured" for k, _d in events)


def test_the_deliverable_contract_carries_its_load_bearing_clauses():
    """The fenced-payload contract appended to a producing stage's prompt: what
    the payload is, what gets measured, and that the producer must LOOK at what
    it made. Each clause is one the engine depends on downstream — the fence is
    parsed, the measurement target is measured, and the self-check is the only
    thing standing between a binary artifact and a text-read 'verification'.

    Its LENGTH is not pinned: that was an anecdote about cost, nothing depends
    on it, and any rewording turned it into noise.
    """
    text = workflow.deliverable_instruction()
    for owed in ("DELIVERABLE PAYLOAD", "MEASUREMENT TARGET",
                 "SELF-CHECK BEFORE HANDOFF", "LOOK at it yourself"):
        assert owed in text


# --------------------------------------------------------------------------- #
# Spend is visible in the path a conversation lives in
# --------------------------------------------------------------------------- #
def test_a_turn_reports_its_own_cost_and_the_conversation_total(tmp_path):
    from brain.observability import Tracer
    from brain.observability.cost import conversation_cost, turn_cost_line

    db = tmp_path / "t.db"
    tracer = Tracer(db, tmp_path / "runs", conversation_id="conv-money")
    first = tracer.start_run("one")
    with tracer.span("agent.session", "a") as sp:
        sp.set(cost_usd=0.25, tokens_in=10, tokens_out=5)
    first.finish("succeeded", outcome="x")
    second = tracer.start_run("two")
    with tracer.span("agent.session", "b") as sp:
        sp.set(cost_usd=1.50, tokens_in=10, tokens_out=5)
    second.finish("succeeded", outcome="y")
    tracer.close()

    total = conversation_cost(db, "conv-money")
    assert round(total["cost_usd"], 2) == 1.75 and total["turns"] == 2

    line = turn_cost_line(db, second.run_id, "conv-money")
    assert "$1.50" in line and "$1.75" in line and "2 turn(s)" in line


def test_cost_reporting_is_fail_open(tmp_path):
    from brain.observability.cost import (conversation_cost, run_cost_line,
                                          turn_cost_line)

    missing = tmp_path / "nope.db"
    assert run_cost_line(missing, "run-x") == ""
    assert turn_cost_line(missing, "run-x", "conv-x") == ""
    assert conversation_cost(missing, "conv-x")["runs"] == 0


def test_the_envelope_does_not_recite_the_whole_library(capsys, monkeypatch):
    """The RENDERED line, not the f-string that produces it: any respelling
    that still dumps the whole list passed the old source check."""
    import asyncio

    from brain import main as main_mod

    class _Summary:
        run_id, status, answer = "run-x", "succeeded", "the answer"
        containers, leaks = 0, 0
        capabilities = ["web_search", "render_artifact_pages", "code_execution"]

    class _Session:
        def add_listener(self, _fn):
            pass

        async def run_objective(self, _o):
            return _Summary()

        def close(self):
            pass

    monkeypatch.setattr(main_mod, "BrainSession", lambda: _Session())
    monkeypatch.setattr(main_mod, "_history_note", lambda *a: "")
    monkeypatch.setattr(main_mod, "_cost_line", lambda _rid: "")
    asyncio.run(main_mod._amain("do the thing", quiet=True))

    out = capsys.readouterr().out
    assert "library=3 capabilit" in out
    for name in _Summary.capabilities:
        assert name not in out
