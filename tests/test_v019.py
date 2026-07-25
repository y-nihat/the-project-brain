"""v0.19 — the defect-repair reach + review-context release (from campaign
c20260715-0608, cycle 2): a run whose analysis was correct died fighting its
own report tool — the worker filed two precise defect reports against the
tool and the repair machinery, which has existed since v0.14, never heard
either one, because the repair pass only ran after the INITIAL stage session
(all reports came from review-driven fix sessions) and because the reporter
named the tool the way its session displays it (mcp__caps__<name>) while the
matcher compared bare capability keys; and a session revision whose final
draft the round-3 reviewer itself called criteria-clean shipped unverified
because three dedup sibling files "create ambiguity about which is the
actual shipped deliverable" — the reviewer was never told which file the
engine designates and ships, and round 2 had already burned a round ordering
the exact reversal of what round 1 demanded, because each round is a fresh
session with no record of the rounds before it."""
from __future__ import annotations

import asyncio

from brain import config, evidence, workflow
from brain.memory.blackboard import Blackboard
from brain.models import AgentSpec, StageSpec
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


class FakeBlackboard:
    def __init__(self):
        self.d: dict = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def put(self, key, value):
        self.d[key] = value


def _orch(tmp_path, runner, planner, session, tracer=None):
    bb = Blackboard(tmp_path / f"{session}.db", session)
    orch = Orchestrator(session=session, manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner, tracer=tracer)
    return orch, bb


def _repair_orch(tmp_path, tracer, defects, reran):
    """A bare orchestrator wired for _maybe_repair_and_rerun in isolation."""
    o = object.__new__(Orchestrator)
    o.tracer = tracer
    o.blackboard = FakeBlackboard()
    o.blackboard.put("tool.defects", defects)
    o.manager = object()
    o.registry = object()
    o.broker = None
    o.model = None
    o._research = None
    o._tool_repairs = 0
    o._insufficient = {}
    o._repaired_caps = set()

    async def rerun(spec, caps, objective, upstream, history, workspace,
                    task_context=None, **_kw):
        reran.append((spec.id, task_context))
        return "retooled output"

    o._run_agent_with_reflection = rerun
    return o


# --- a defect reported from a fix session reaches the repair machinery -------------- #
def test_mcp_display_name_matches_the_held_capability():
    caps = {"make_report": object()}
    assert Orchestrator._reported_cap("make_report", caps) == "make_report"
    assert Orchestrator._reported_cap("mcp__caps__make_report", caps) == "make_report"
    assert Orchestrator._reported_cap("mcp__caps__other_tool", caps) == ""
    assert Orchestrator._reported_cap("", caps) == ""


def test_fix_session_defect_report_triggers_repair_and_rerun(tmp_path, monkeypatch):
    """The t4-c2 shape: the defect was reported inside a review fix session
    under the MCP display name. Previously it matched nothing and the review
    burned every round regenerating through the same broken tool."""
    import brain.orchestrator as orch_mod

    tracer = RecordingTracer()
    reran: list[tuple] = []
    repaired: list[str] = []
    old_cap, new_cap = object(), object()

    async def fake_repair(cap, reason, **_kw):
        assert cap is old_cap and "appends" in reason
        repaired.append(reason)
        return new_cap

    monkeypatch.setattr(orch_mod, "acquire_repair", fake_repair)
    defects = [{"agent": "author.fix", "tool": "mcp__caps__make_report",
                "error": "the tool appends an extra unrequested section"}]
    o = _repair_orch(tmp_path, tracer, defects, reran)
    caps = {"make_report": old_cap}
    stage = StageSpec(id="author", pattern="single", role="A", goal="write")
    spec = AgentSpec(id="author.fix", role="A", goal="write")
    out = asyncio.run(o._maybe_repair_and_rerun(
        stage, spec, caps, "degraded draft", "obj", {}, None, tmp_path, None))
    assert out == "retooled output"
    assert repaired and caps["make_report"] is new_cap
    assert reran and reran[0][0] == "author.fix.retool"
    assert "stage.rerun_after_repair" in tracer.kinds()
    assert all(d.get("handled") for d in o.blackboard.get("tool.defects"))


def test_defect_against_an_unheld_tool_is_left_alone(tmp_path, monkeypatch):
    import brain.orchestrator as orch_mod

    async def fake_repair(*_a, **_kw):  # pragma: no cover — must not run
        raise AssertionError("repair must not be attempted")

    monkeypatch.setattr(orch_mod, "acquire_repair", fake_repair)
    reran: list[tuple] = []
    defects = [{"agent": "author.fix", "tool": "mcp__team__report_blocker",
                "error": "not a capability"}]
    o = _repair_orch(tmp_path, RecordingTracer(), defects, reran)
    stage = StageSpec(id="author", pattern="single", role="A", goal="write")
    spec = AgentSpec(id="author.fix", role="A", goal="write")
    out = asyncio.run(o._maybe_repair_and_rerun(
        stage, spec, {"make_report": object()}, "draft", "obj", {}, None,
        tmp_path, None))
    assert out == "draft" and not reran
    assert not o.blackboard.get("tool.defects")[0].get("handled")


def test_redo_closure_runs_the_repair_pass(tmp_path, monkeypatch):
    """The review's fix sessions flow through the redo closure — the repair
    pass must run there, not only after the initial stage session."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    calls: list[str] = []

    async def recorder(self, stage, spec, caps, out, *a, **kw):
        calls.append(spec.id)
        return out

    monkeypatch.setattr(Orchestrator, "_maybe_repair_and_rerun", recorder)

    async def runner(_t, name, prompt, options):
        if name == "draft":
            return "```deliverable\nthe work\n```"
        if name == "draft.fix":
            return "```deliverable\nthe work, fixed\n```"
        if name == "check.r1":
            return "VERDICT: revise\n1. fix the intro"
        return "VERDICT: approved\nok"

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write",
                   deliverable=True),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], rounds=2)])
    orch, bb = _orch(tmp_path, runner, planner, "redoreach1", RecordingTracer())
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    assert "draft" in calls        # after the initial session (unchanged)
    assert "draft.fix" in calls    # after the review-driven fix session (new)


# --- the reviewer is told the deliverable of record and its dedup family ------------- #
def _artifact(tmp_path, session, name, content, role="artifact"):
    run = tmp_path / "runs" / session
    ws = run / "workspace"
    (ws / "make_report").mkdir(parents=True, exist_ok=True)
    (ws / f"make_report/{name}").write_text(content)
    evidence.record(run, "make_report", [f"make_report/{name}"], [], "complete",
                    role=role, workspace=ws)
    return ws


def test_dedup_family_groups_collision_ordinals():
    fam = Orchestrator._dedup_family
    assert fam("make_report/report.md") == fam("make_report/report-2.md")
    assert fam("make_report/report-2.md") == fam("make_report/report-13.md")
    assert fam("make_report/report.md") != fam("make_report/summary.md")
    assert fam("a/report.md") != fam("b/report.md")
    assert fam("make_report/report.md") != fam("make_report/report.html")


def test_artifact_of_record_names_superseded_siblings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    ws = _artifact(tmp_path, "recsib1", "report.md", "# one\ntext")
    _artifact(tmp_path, "recsib1", "report-2.md", "# two\ntext")
    _artifact(tmp_path, "recsib1", "summary.md", "# other artifact")
    o = object.__new__(Orchestrator)
    chosen, superseded = o._artifact_of_record(
        "Final deliverable: make_report/report-2.md", ws)
    assert chosen == "make_report/report-2.md"
    assert superseded == ["make_report/report.md"]   # summary.md is NOT debris


def test_review_context_designates_the_record_and_settles_siblings(
        tmp_path, monkeypatch):
    """The t5-c2 r3 shape: the reviewer's own verdict called the final draft
    criteria-clean, then refused approval because sibling dedup files exist
    and ordered deletions no session can perform. The round context must name
    the shipped file and settle the siblings."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    prompts: dict[str, str] = {}

    async def runner(_t, name, prompt, options):
        prompts[name] = prompt
        if name == "draft":
            _artifact(tmp_path, "recctx1", "report.md", "# v1\ndraft text")
            _artifact(tmp_path, "recctx1", "report-2.md", "# v2\nfinal text")
            return "```deliverable\nShipped file: make_report/report-2.md\n```"
        return "VERDICT: approved\nok"

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write",
                   deliverable=True),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], rounds=1)])
    orch, bb = _orch(tmp_path, runner, planner, "recctx1", RecordingTracer())
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    ctx = prompts["check.r1"]
    assert "DELIVERABLE OF RECORD: make_report/report-2.md" in ctx
    assert "SUPERSEDED WORKING DRAFTS" in ctx
    assert "make_report/report.md" in ctx
    assert "NOT grounds to revise or withhold approval" in ctx


# --- review rounds see the rounds before them ---------------------------------------- #
def test_later_rounds_receive_the_prior_verdict_record(tmp_path, monkeypatch):
    """The t5-c2 r1/r2 shape: round 2, a fresh session, ordered the exact
    reversal of the fix round 1 had demanded and the author had applied.
    Later rounds must see what was already demanded and settled."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    prompts: dict[str, str] = {}

    async def runner(_t, name, prompt, options):
        prompts[name] = prompt
        if name == "draft":
            return "```deliverable\nthe work\n```"
        if name == "draft.fix":
            return "```deliverable\nthe work, with a tightened intro\n```"
        if name == "check.r1":
            return "VERDICT: revise\n1. tighten the intro"
        return "VERDICT: approved\nok"

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write",
                   deliverable=True),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], rounds=2)])
    orch, bb = _orch(tmp_path, runner, planner, "continuity1", RecordingTracer())
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    assert "PRIOR REVIEW ROUNDS" not in prompts["check.r1"]
    r2 = prompts["check.r2"]
    assert "PRIOR REVIEW ROUNDS" in r2
    assert "tighten the intro" in r2          # what round 1 demanded
    assert "REVIEW CONTINUITY" in r2
    assert "SETTLED" in r2


# --- owner-review amendments on top of the adopted v0.19 proposals ------------------ #
def test_referenced_siblings_are_never_superseded(tmp_path, monkeypatch):
    """A deliverable may legitimately be MANY files the payload names
    (chapters, parts) that share a dedup family; only UNREFERENCED same-family
    iterations are debris. Labeling a referenced part 'superseded working
    draft' would tell the reviewer to ignore shipped content."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    run = tmp_path / "runs" / "multi1"
    ws = run / "workspace"
    (ws / "write_md").mkdir(parents=True)
    for name in ("part.md", "part-2.md", "part-3.md"):
        (ws / "write_md" / name).write_text("## s\n\nbody")
        evidence.record(run, "write_md", [f"write_md/{name}"], [], "complete",
                        role="artifact", workspace=ws)
    orch = object.__new__(Orchestrator)
    orch.tracer = NullTracer()
    payload = "The deliverable is write_md/part.md plus write_md/part-3.md."
    chosen, superseded = orch._artifact_of_record(payload, ws)
    assert chosen == "write_md/part-3.md"
    assert superseded == ["write_md/part-2.md"]   # referenced part.md is NOT debris
def test_agent_prompt_names_the_defect_channel():
    from brain import factory
    from brain.models import AgentSpec
    text = factory.agent_system_prompt(
        AgentSpec(id="a", role="R", goal="g", capabilities=[]),
        ["mcp__caps__t"], team_stages=["s1"])
    assert "report_tool_defect" in text
    assert "insufficient tool" in text


def test_planner_revision_scope_propagates_changed_facts():
    """Pin the RULE, not the label. The heading survived every rewrite; the
    body underneath it — the part that actually says what propagation means —
    could be deleted entirely and the old one-line assertion still passed.

    (The heading itself is pinned once, in
    tests/test_recovery.py::test_planner_contract_sections_have_not_been_deleted.)
    """
    from brain import planner as planner_mod

    s = planner_mod._SYSTEM
    for clause in ("figures derived from it", "restatements of it",
                   "encode that propagation in the criteria"):
        assert clause in s, f"the propagation rule lost {clause!r}"
