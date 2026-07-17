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


# --- v0.22: scoped counts + coverage-gap escalation --------------------------------- #
def test_scoped_count_measures_within_its_section():
    """An approved audit was voided at its final round because 'Section 1 -
    one paragraph' was measured as nineteen paragraphs of full document: a
    scoped bound must be measured within its identifiable part."""
    from brain import criteria
    doc = "# T\n\n## What it does\n\none tight paragraph\n\n## Defects\n\na\n\nb\n\nc"
    c = criteria.check_counts(["Section 1 contains exactly one paragraph"], doc)[0]
    assert c.scope == "section 1" and c.measured == 1 and c.ok and c.gating
    v = criteria.check_counts(["Section 2 is exactly one paragraph"], doc)[0]
    assert v.measured == 3 and not v.ok and v.gating


def test_unresolvable_scope_is_advisory_never_gating():
    from brain import criteria
    missing = criteria.check_counts(["Section 9 has exactly one paragraph"],
                                    "## A\n\nx")[0]
    assert not missing.gating and "not found" in missing.scope
    per = criteria.check_counts(["each answer is at most 40 words"],
                                "word " * 300)[0]
    assert not per.gating and "per-item" in per.scope
    glob = criteria.check_counts(["exactly 1 section"], "## A\n\nx")[0]
    assert glob.gating and glob.scope == ""


def test_agent_contract_escalates_required_coverage_gaps():
    from brain import factory
    from brain.models import AgentSpec
    text = factory.agent_system_prompt(
        AgentSpec(id="a", role="R", goal="g", capabilities=[]),
        ["mcp__caps__t"], team_stages=["s1"])
    assert "COVERAGE GAP" in text and "re-observation" in text


def test_reviewer_contract_gates_lossy_perception_identity(tmp_path, monkeypatch):
    """Two surnames wrong, one flagged, review approved: lookup tools cannot
    re-adjudicate audio, so flagged/inconsistent identity tokens must cap
    their criterion at 'unknown' — approval must not rest on them."""
    from brain.memory.blackboard import Blackboard
    from brain.planner import StubPlanner
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    prompts = {}

    async def runner(_t, name, prompt, options):
        prompts[name] = prompt
        if name.startswith("check.r"):
            return "VERDICT: approved\nok"
        return "```deliverable\nthe work\n```"

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write",
                   deliverable=True),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], rounds=2)])
    bb = Blackboard(tmp_path / "lp.db", "lossy1")
    orch = Orchestrator(session="lossy1", manager=None, ledger=None,
                        registry=None, blackboard=bb, broker=None,
                        planner=planner, resolver=None, session_runner=runner,
                        tracer=NullTracer())
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    ctx = prompts.get("check.r1", "")
    assert "LOSSY-PERCEPTION IDENTITY RULE" in ctx
    assert "never met" in ctx


# --- v0.23.1: binary-vs-text input nature (campaign-4 t6 cycle-1) -------------------- #
def test_input_nature_flags_binary_from_example(tmp_path):
    from brain.acquisition import _describe_input_files
    ws = tmp_path / "ws"; (ws / "input").mkdir(parents=True)
    (ws / "input" / "export.aslog").write_bytes(bytes([0xA5, 0x01]) + b"\x00\x10" * 64)
    (ws / "input" / "notes.md").write_text("plain text notes, valid utf-8")
    out = _describe_input_files({"files": ["input/export.aslog", "input/notes.md"]}, ws)
    assert "input/export.aslog: BINARY" in out and "a5 01" in out
    assert "open 'rb'" in out and "struct.unpack" in out
    assert "input/notes.md: UTF-8 TEXT" in out


def test_input_nature_falls_back_to_run_inputs_in_repair(tmp_path):
    """Repair carries no example_input; the run's actual input files must still
    be described so a reseed doesn't inherit a text misperception."""
    from brain.acquisition import _describe_input_files
    ws = tmp_path / "ws"; (ws / "input").mkdir(parents=True)
    (ws / "input" / "telemetry.bin").write_bytes(b"\xff\xfe\x00\x01" * 40)
    out = _describe_input_files(None, ws)
    assert "telemetry.bin: BINARY" in out and "do NOT decode" in out


def test_input_nature_silent_without_workspace_or_files():
    from brain.acquisition import _describe_input_files
    assert _describe_input_files({"files": ["x"]}, None) == ""
    assert _describe_input_files({}, None) == ""


def test_developer_contract_has_binary_vs_text_rule():
    from brain.acquisition import _DEVELOPER_SYSTEM
    assert "BINARY VS TEXT" in _DEVELOPER_SYSTEM
    assert "struct.unpack" in _DEVELOPER_SYSTEM


# --- v0.23.2: repaired tools quarantined cross-run (campaign-4 t6 cycle-2) ----------- #
def test_repaired_tool_quarantined_at_run_end(tmp_path):
    """A broken parser reused-and-repaired every cycle never got a clean
    re-author because the patch reseeds the broken approach. A tool that
    needed mid-run repair is quarantined so the next run re-authors it.
    The run-end sweep quarantines every capability in _repaired_caps."""
    quarantined = []

    class Reg:
        def get(self, n): return type("C", (), {"description": "parse a file"})()
        def quarantine(self, name, reason): quarantined.append((name, reason))

    orch = object.__new__(Orchestrator)
    orch.tracer = NullTracer()
    orch.registry = Reg()
    orch._repaired_caps = {"parse_aslog_export", "ocr_cert"}
    # exercise the run-end sweep body directly
    for name in sorted(orch._repaired_caps):
        orch._quarantine_insufficient(name, "required mid-run repair")
    names = {n for n, _ in quarantined}
    assert names == {"parse_aslog_export", "ocr_cert"}, quarantined
    assert all("insufficient" in r for _, r in quarantined)


def test_quarantine_insufficient_is_best_effort(tmp_path):
    orch = object.__new__(Orchestrator)
    orch.tracer = NullTracer()
    orch.registry = None
    orch._quarantine_insufficient("x", "reason")  # no registry -> no crash


def test_agent_contract_has_citation_integrity_rule():
    from brain import factory
    from brain.models import AgentSpec
    t = factory.agent_system_prompt(
        AgentSpec(id="a", role="R", goal="g", capabilities=[]), ["mcp__caps__t"])
    assert "CITATION INTEGRITY" in t
    assert "ACTUALLY RESOLVED" in t and "not-found/404" in t
    assert "SCOPE each evidence item" in t
    assert "output contract's allowed source values" in t
