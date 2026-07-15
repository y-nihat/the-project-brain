"""v0.21 — capability re-entry + windowed perception (campaign-3 cycle-1
owner review): a run filed ten precise defect reports against a cached tool
whose computation menu froze a prior task's 7-day window, spent both repairs
without clearing the class, and could only BLOCK — honest, but the loop
never authored a DIFFERENT tool for the same data, because tool-defect
exhaustion had no path into strategy re-entry; agents across three tasks
filed defect reports against read_file itself for its flat 8000-char clip
on files they demonstrably needed whole; and a speech-to-text tool chose
speed over fidelity, rendering an attendee as 'tennis carter'."""
from __future__ import annotations

import asyncio

from brain import config
from brain.memory.blackboard import Blackboard
from brain.models import StageSpec
from brain.observability import NullTracer
from brain.orchestrator import CapabilityInsufficientError, Orchestrator


class RecordingTracer(NullTracer):
    def __init__(self):
        self.events = []

    def event(self, kind, subject, status="info", **data):
        self.events.append((kind, subject, status, data))

    def kinds(self):
        return [k for k, *_ in self.events]


def _bare(tracer=None, bb=None):
    o = object.__new__(Orchestrator)
    o.tracer = tracer or NullTracer()
    o.blackboard = bb
    o.manager = object()
    o.registry = object()
    o._tool_repairs = 0
    o._insufficient = {}
    o._repaired_caps = set()
    return o


def test_recurred_defect_marks_capability_insufficient(tmp_path):
    tracer = RecordingTracer()
    bb = Blackboard(tmp_path / "a.db", "s1")
    try:
        bb.put("tool.defects", [{"agent": "analyze.w01", "tool": "crunch",
                                 "error": "window is hardcoded to 7 days"}])
        orch = _bare(tracer, bb)
        orch._repaired_caps.add("crunch")
        stage = StageSpec(id="analyze", pattern="single", role="A", goal="g")
        out = asyncio.run(orch._maybe_repair_and_rerun(
            stage, None, {"crunch": object()}, "draft", "obj", {}, None,
            tmp_path, None))
        assert out == "draft"
        assert orch._insufficient["analyze"][0] == "crunch"
        assert "RECURRED" in orch._insufficient["analyze"][1]
        assert "capability.insufficient" in tracer.kinds()
    finally:
        bb.close()


def test_budget_exhaustion_marks_capability_insufficient(tmp_path):
    tracer = RecordingTracer()
    bb = Blackboard(tmp_path / "b.db", "s2")
    try:
        bb.put("tool.defects", [{"agent": "analyze", "tool": "crunch",
                                 "error": "cannot express the 30-day window"}])
        orch = _bare(tracer, bb)
        orch._tool_repairs = config.TOOL_DEFECT_REPAIRS_PER_RUN
        stage = StageSpec(id="analyze", pattern="single", role="A", goal="g")
        asyncio.run(orch._maybe_repair_and_rerun(
            stage, None, {"crunch": object()}, "draft", "obj", {}, None,
            tmp_path, None))
        assert orch._insufficient["analyze"][0] == "crunch"
        assert "capability.defect_unrepaired" in tracer.kinds()
    finally:
        bb.close()


def test_method_outcome_maps_insufficiency():
    orch = object.__new__(Orchestrator)
    orch._workspace_files = lambda ws: []
    stage = StageSpec(id="analyze", pattern="single", role="A", goal="compute")
    out = orch._method_outcome(
        stage, CapabilityInsufficientError("crunch", "analyze", "frozen window"),
        None)
    assert out.failure_class == "capability_insufficient"
    assert out.capability == "crunch"
    assert out.retryable


def test_insufficient_stage_enters_recovery(tmp_path, monkeypatch):
    """End to end: the stage completes, the insufficiency marker raises, and
    the EXISTING recovery path replaces the node via planner extension —
    the same adjudicator that serves failures and gaps."""
    from brain.planner import StubPlanner

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()

    class ExtendingPlanner(StubPlanner):
        async def extend(self, objective, completed, gaps, existing_ids):
            return ([StageSpec(id="analyze2", pattern="single", role="A",
                               goal="compute with a NEW tool contract",
                               deliverable=True)], None)

    async def runner(_t, name, prompt, options):
        return f"```deliverable\nresult({name})\n```"

    planner = ExtendingPlanner([StageSpec(id="analyze", pattern="single",
                                          role="A", goal="compute",
                                          deliverable=True)])
    bb = Blackboard(tmp_path / "c.db", "rec1")
    orch = Orchestrator(session="rec1", manager=None, ledger=None,
                        registry=None, blackboard=bb, broker=None,
                        planner=planner, resolver=None, session_runner=runner,
                        tracer=tracer)
    real = orch._run_stage

    async def wrapped(stage, *a, **k):
        out = await real(stage, *a, **k)
        if stage.id == "analyze":
            orch._insufficient["analyze"] = ("crunch", "frozen parameter")
        return out

    orch._run_stage = wrapped
    try:
        answer = asyncio.run(orch.run("compute the thing"))
    finally:
        bb.close()
    assert "analyze2" in answer or "result(analyze2)" in answer
    assert "crunch" in orch._suppressed_caps
    assert "stage.failed" in tracer.kinds()
    failed = [d for k, s, _st, d in tracer.events if k == "stage.failed"]
    assert failed[0].get("failure_class") == "capability_insufficient"


def test_read_file_windows(tmp_path):
    orch = object.__new__(Orchestrator)
    orch.tracer = NullTracer()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "big.txt").write_text("A" * 9000 + "B" * 9000 + "C" * 2000)
    r0 = orch._read_workspace(ws, "big.txt")["content"][0]["text"]
    assert "chars 0-8,000 of 20,000" in r0
    assert "offset=8000" in r0
    r1 = orch._read_workspace(ws, "big.txt", offset=8000)["content"][0]["text"]
    assert "chars 8,000-16,000" in r1 and r1.count("B") > 6000
    r2 = orch._read_workspace(ws, "big.txt", offset=16000)["content"][0]["text"]
    assert "chars 16,000-20,000" in r2 and "offset=" not in r2
    beyond = orch._read_workspace(ws, "big.txt", offset=99999)["content"][0]["text"]
    assert "beyond the end" in beyond
    (ws / "small.txt").write_text("hello")
    small = orch._read_workspace(ws, "small.txt")["content"][0]["text"]
    assert small.startswith("[small.txt]\n")


def test_authoring_contract_parameterization_and_fidelity():
    from brain.acquisition import _DEVELOPER_SYSTEM
    assert "SUPPLIED-QUANTITY PARAMETERIZATION" in _DEVELOPER_SYSTEM
    assert "PERCEPTION FIDELITY" in _DEVELOPER_SYSTEM
    assert "NEVER a constant baked into the code" in _DEVELOPER_SYSTEM
