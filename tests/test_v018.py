"""v0.18 — the review-targeting + designation release (from campaign
c20260715-0608, cycle 1): the review engine assumed its first dependency was
the draft under review, but the planner contract never promised that order —
two runs with review-verified content burned every round judging a
prerequisite stage's status report and routing revisions to an agent who
rightly answered "not my task", while the marked deliverable stage's draft
shipped unreviewed; the artifact-of-record rule measured whichever
measurable artifact was produced LAST, so template-probe placeholders
produced after the real artifact were what a revision remeasure and the
run-end measurement certified; a session's second turn blocked honestly
because the prior turn's deliverable — the very thing the feedback was
about — was never seeded (the planner inlined its text into a stage goal,
defeating both the LLM revision judgment and the prior-path fallback); and
a review-round revision regenerated its numbers from memory, fabricating
values its own round-3 reviewer then caught."""
from __future__ import annotations

import asyncio

from brain import config, evidence, workflow
from brain.memory.blackboard import Blackboard
from brain.models import StageSpec
from brain.observability import NullTracer
from brain.orchestrator import Orchestrator
from brain.planner import StubPlanner


class RecordingTracer(NullTracer):
    def __init__(self):
        self.events: list[tuple] = []

    def event(self, kind, subject, status="info", **data):
        self.events.append((kind, subject, status, data))

    def kinds(self):
        return [k for k, *_ in self.events]


def _orch(tmp_path, runner, planner, session, tracer=None, supervisor=None):
    bb = Blackboard(tmp_path / f"{session}.db", session)
    orch = Orchestrator(session=session, manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner, tracer=tracer, supervisor=supervisor)
    return orch, bb


def _bare_orch(tracer=None):
    o = object.__new__(Orchestrator)
    o.tracer = tracer or NullTracer()
    o._criteria = []
    o._stage_notes = {}
    o._presubmit_fixes_left = 1
    o._revision_fixups_left = 1
    o._gate_fixups_left = 1
    return o


# --- the review subject is the marked deliverable, not depends_on[0] ---------------- #
def test_review_subject_prefers_the_marked_deliverable_dependency():
    stages = [StageSpec(id="prereq", pattern="single", role="P", goal="prepare"),
              StageSpec(id="author", pattern="single", role="A", goal="write",
                        depends_on=["prereq"], deliverable=True),
              StageSpec(id="check", pattern="review", role="C", goal="verify",
                        depends_on=["prereq", "author"])]
    assert workflow.review_subject(stages[2], stages) == "author"


def test_review_subject_falls_back_to_first_dependency():
    stages = [StageSpec(id="a", pattern="single", role="A", goal="one"),
              StageSpec(id="b", pattern="single", role="B", goal="two",
                        depends_on=["a"]),
              StageSpec(id="check", pattern="review", role="C", goal="verify",
                        depends_on=["a", "b"])]
    assert workflow.review_subject(stages[2], stages) == "a"
    bare = StageSpec(id="check", pattern="review", role="C", goal="verify")
    assert workflow.review_subject(bare, stages) is None


def test_review_judges_and_revises_the_deliverable_stage(tmp_path, monkeypatch):
    """The t1/t2-c1 shape: depends_on listed the prerequisite first; every
    verdict round judged the prerequisite's report as 'the draft' and every
    revision session went to its agent. The review must judge, and route
    revisions to, the marked deliverable stage."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    sessions: list[str] = []
    fix_prompts: list[str] = []

    async def runner(_t, name, prompt, options):
        sessions.append(name)
        if name == "prereq":
            return "the prereq inventory"
        if name == "author":
            return "```deliverable\nthe memo\n```"
        if name.startswith("check.r"):
            assert "the memo" in prompt          # the draft under review
            assert "the prereq inventory" in prompt   # upstream evidence
            if name == "check.r1":
                return "VERDICT: revise\n1. tighten the intro"
            return "VERDICT: approved\ngood"
        if name == "author.fix":
            fix_prompts.append(prompt)
            return "```deliverable\nthe memo, tightened\n```"
        raise AssertionError(f"unexpected session {name}")

    planner = StubPlanner(
        [StageSpec(id="prereq", pattern="single", role="P", goal="prepare"),
         StageSpec(id="author", pattern="single", role="A", goal="write",
                   depends_on=["prereq"], deliverable=True),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["prereq", "author"], rounds=2)])
    orch, bb = _orch(tmp_path, runner, planner, "target1", tracer)
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    assert "author.fix" in sessions
    assert "prereq.fix" not in sessions
    assert orch._review_approved
    assert any(k == "review.revised" and s == "author"
               for k, s, *_ in tracer.events)


def test_revision_prompt_demands_minimal_edits(tmp_path, monkeypatch):
    """The t4-c1 round-2 redo regenerated its figures from memory and
    fabricated values round 3 then caught — the fix instruction must bind
    the reviser to verbatim carry-over of everything the critique does not
    name."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    seen: list[str] = []

    async def runner(_t, name, prompt, options):
        if name == "draft":
            return "```deliverable\nthe work\n```"
        if name == "draft.fix":
            seen.append(prompt)
            return "```deliverable\nthe work, fixed\n```"
        if name == "check.r1":
            return "VERDICT: revise\n1. fix the intro"
        return "VERDICT: approved\nok"

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write",
                   deliverable=True),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], rounds=2)])
    orch, bb = _orch(tmp_path, runner, planner, "minimal1", RecordingTracer())
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    assert seen and "MINIMAL EDIT RULE" in seen[0]
    assert "VERBATIM" in seen[0]


# --- the payload's own reference designates the deliverable of record --------------- #
def _artifact(tmp_path, session, name, content, role="artifact"):
    run = tmp_path / "runs" / session
    ws = run / "workspace"
    (ws / "make_report").mkdir(parents=True, exist_ok=True)
    (ws / f"make_report/{name}").write_text(content)
    evidence.record(run, "make_report", [f"make_report/{name}"], [], "complete",
                    role=role, workspace=ws)
    return ws


def test_measurement_follows_the_payload_referenced_artifact(tmp_path, monkeypatch):
    """The t4-c1 shape: probe/placeholder siblings produced AFTER the real
    artifact (collision-deduped names guarantee they accumulate) made
    'last-produced wins' measure a placeholder — in a revision remeasure and
    again at run end. The payload named exactly one produced file; that
    designation outranks recency."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    real = ("<h1>Fleet Health</h1>" +
            "".join(f"<section><h2>S{i}</h2><p>data</p></section>" for i in range(4)))
    ws = _artifact(tmp_path, "designate1", "report.html", real)
    _artifact(tmp_path, "designate1", "report-2.html",
              "<section><h2>Test Section</h2><p>placeholder</p></section>")
    orch = _bare_orch()
    orch._criteria = ["exactly 4 sections"]
    checks, target = orch._measured_checks(
        "Final report: make_report/report.html", ws)
    assert "report.html" in target and "report-2" not in target
    assert checks[0].measured == 4 and checks[0].ok


def test_unreferencing_payload_keeps_the_last_produced_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    _artifact(tmp_path, "designate2", "report.html", "<h2>a</h2>")
    ws = _artifact(tmp_path, "designate2", "report-2.html",
                   "<h2>a</h2><h2>b</h2>")
    orch = _bare_orch()
    orch._criteria = ["exactly 2 sections"]
    checks, target = orch._measured_checks("the analysis is done", ws)
    assert "report-2.html" in target
    assert checks[0].measured == 2 and checks[0].ok


def test_reference_by_bare_filename_designates(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    ws = _artifact(tmp_path, "designate3", "report.html",
                   "<h2>a</h2><h2>b</h2>")
    _artifact(tmp_path, "designate3", "report-2.html", "<h2>only</h2>")
    orch = _bare_orch()
    orch._criteria = ["exactly 2 sections"]
    checks, target = orch._measured_checks(
        "See report.html for the full breakdown.", ws)
    assert "make_report/report.html" in target
    assert checks[0].ok


# --- a session turn always seeds the prior deliverable ------------------------------- #
def test_session_turn_seeds_the_prior_deliverable_without_any_detector(
        tmp_path, monkeypatch):
    """t5-c1 turn 2: no plan.revision (again), and no 'prior/' path in the
    plan because the planner INLINED the prior deliverable's text into the
    stage goal — uncitable narration. The engine held the baseline and knew
    this run continued a session; a preserve-discipline review blocked for
    want of a file the engine could have placed."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    prior = tmp_path / "runs" / "prev-run"
    (prior / "outputs").mkdir(parents=True)
    (prior / "workspace").mkdir()
    (prior / "outputs" / "DELIVERABLE.md").write_text("### Q: one\nanswer\n")
    tracer = RecordingTracer()

    async def runner(_t, name, prompt, options):
        return "revised"

    planner = StubPlanner([StageSpec(
        id="revise", pattern="single", role="E",
        goal="update the two affected answers; the prior text is: ...")])
    orch, bb = _orch(tmp_path, runner, planner, "sessturn1", tracer)
    try:
        asyncio.run(orch.run("the details changed; update the document",
                             seed_run="prev-run",
                             session_context="previous run id: prev-run"))
    finally:
        bb.close()
    ws = tmp_path / "runs" / "sessturn1" / "workspace"
    assert (ws / "prior" / "prev-run" / "outputs" / "DELIVERABLE.md").is_file()
    assert "revision.deliverable_seeded" in tracer.kinds()
