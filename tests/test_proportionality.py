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
def test_every_deliverable_gate_is_conditioned_on_the_verdict():
    import inspect

    from brain import orchestrator as orch_mod

    # Join line continuations so a wrapped condition reads as one statement.
    src = inspect.getsource(orch_mod).replace("\\\n", " ").replace("(\n", "(")
    # Every place that asks "is this the deliverable stage?" now asks the plan
    # first. An unconditioned one would reintroduce the fixed machinery.
    for line in src.splitlines():
        if "workflow.deliverable_stage_ids(" not in line:
            continue
        if "deliv_ids = " in line:
            continue        # routing: which stage OWNS it, if there is one
        assert "_plan_makes_artifact" in line, line


def test_the_deliverable_contract_is_not_free():
    """It is ~1.4 KB of counting, fencing and self-render rules appended to the
    producing stage's prompt — worth it for an artifact, pure cost without one."""
    text = workflow.deliverable_instruction()
    assert len(text) > 1000
    for owed in ("DELIVERABLE PAYLOAD", "MEASUREMENT TARGET", "SELF-CHECK BEFORE HANDOFF"):
        assert owed in text


def test_the_run_records_which_way_it_went():
    import inspect

    from brain import orchestrator as orch_mod

    src = inspect.getsource(orch_mod.Orchestrator.run)
    assert "plan.artifact_of_record" in src


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


def test_the_envelope_does_not_recite_the_whole_library():
    """Naming every promoted capability on every run buried the run's own
    accounting under a list that has nothing to do with it."""
    import inspect

    from brain import main as main_mod

    src = inspect.getsource(main_mod._amain)
    assert "capabilities={summary.capabilities}" not in src
    assert "len(summary.capabilities)" in src
