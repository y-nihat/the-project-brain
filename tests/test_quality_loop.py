"""The quality loop (V0120 board-pack run): every adaptation trigger was
failure-shaped — a run could be grounded, criteria-clean, and still amateur,
because 'what does good look like?' was never asked, nobody ever LOOKED at the
audience-facing artifact, and a review died wholesale when one of its tools
failed to build. These pin the four additions: declared knowledge needs
resolved through research into ONE informed replan; perceptual review (render
the artifact, look at every page, engine-measured page counts); session
revision memory (feedback revises the previous deliverable instead of starting
over); and per-capability degradation on the review path."""
from __future__ import annotations

import asyncio
import json

from brain import acquisition, config, criteria, evidence, workflow
from brain.memory.blackboard import Blackboard
from brain.models import Plan, StageSpec
from brain.observability import NullTracer
from brain.orchestrator import (_RENDER_NEED_KEY, _RENDERABLE_EXTS, Orchestrator)
from brain.planner import StubPlanner
from brain.sandbox.manager import RunResult


class RecordingTracer(NullTracer):
    def __init__(self):
        self.events: list[tuple] = []

    def event(self, kind, subject, status="info", **data):
        self.events.append((kind, subject, status, data))

    def kinds(self):
        return [k for k, *_ in self.events]


def _orch(tmp_path, runner, planner, session, tracer=None):
    bb = Blackboard(tmp_path / f"{session}.db", session)
    orch = Orchestrator(session=session, manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner, tracer=tracer)
    return orch, bb


def _produce(tmp_path, session, cap, name, role, content=b"bytes",
             refs=("args",)):
    """Write a file the way the capability runtime does: bytes + a provenance
    record carrying the tool's DECLARED role."""
    d = tmp_path / "runs" / session / "workspace" / cap
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(content)
    evidence.record(tmp_path / "runs" / session, cap, [f"{cap}/{name}"],
                    list(refs), "complete", role=role)
    return f"{cap}/{name}"


# --- P1: declared knowledge needs -> research -> ONE informed replan --------------- #
def test_knowledge_needs_research_one_informed_replan(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    prompts: dict[str, str] = {}
    researched: list[tuple] = []

    async def runner(_t, name, prompt, options):
        prompts[name] = prompt
        return "```deliverable\nthe work\n```"

    planner = StubPlanner(
        [StageSpec(id="make", pattern="single", role="M", goal="produce it")],
        knowledge_needs=[{"topic": "layout conventions for the deliverable form",
                          "why": "audience-judged; objective does not define good"}])
    orch, bb = _orch(tmp_path, runner, planner, "know1", tracer)

    async def fake_research(topic, why="", specifics=""):
        researched.append((topic, why))
        return "METHOD KNOWLEDGE: grid alignment; one message per unit."

    orch._research = fake_research
    try:
        asyncio.run(orch.run("make the thing"))
    finally:
        bb.close()
    # Researched, then replanned exactly once with the briefs as corrective.
    assert researched and "layout conventions" in researched[0][0]
    assert planner.plan_calls == 2
    assert "KNOWLEDGE BRIEFS" in planner.correctives[-1]
    assert "grid alignment" in planner.correctives[-1]
    # StubPlanner mirrors the real contract: the informed replan does not
    # redeclare needs, so there is no research loop.
    assert "knowledge.researching" in tracer.kinds()
    assert "knowledge.informed" in tracer.kinds()
    # The briefs reach the deliverable-producing stage as the quality bar.
    assert "RESEARCHED QUALITY STANDARDS" in prompts["make"]
    assert "grid alignment" in prompts["make"]


def test_knowledge_replan_failure_is_fail_open(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()

    class BrittlePlanner(StubPlanner):
        async def plan(self, objective, history=None, corrective=None,
                       session_context=None):
            if corrective:
                raise RuntimeError("informed replan died")
            return await super().plan(objective, history=history,
                                      session_context=session_context)

    async def runner(_t, name, prompt, options):
        return "done"

    planner = BrittlePlanner(
        [StageSpec(id="make", pattern="single", role="M", goal="produce it")],
        knowledge_needs=[{"topic": "standards", "why": "quality"}])
    orch, bb = _orch(tmp_path, runner, planner, "know2", tracer)

    async def fake_research(topic, why="", specifics=""):
        return "brief"

    orch._research = fake_research
    try:
        answer = asyncio.run(orch.run("make the thing"))
    finally:
        bb.close()
    assert "knowledge.replan_failed" in tracer.kinds()
    assert "done" in answer                    # original plan still executed


def test_formatted_prompt_templates_actually_render():
    """Prompt templates that go through str.format() must RENDER, not just
    contain the right words: an unescaped literal brace is a KeyError that
    kills every run at the first plan call (observed live on v0.13.0 —
    '({topic, why})' parsed as a replacement field named 'topic, why'; the
    suite stayed green because nothing executed the real template)."""
    import string

    from brain import acquisition as acq_mod
    from brain import planner as planner_mod

    rendered = planner_mod._SYSTEM.format(known="CAP CATALOG")
    assert "CAP CATALOG" in rendered
    assert "{topic, why}" in rendered            # escaped braces survive as text
    assert "{of_run" in rendered
    assert acq_mod._BUILDER_SYSTEM.format(free_hint="FREE")
    # Every declared field in each template is one we actually supply.
    for tpl, allowed in ((planner_mod._SYSTEM, {"known"}),
                         (acq_mod._BUILDER_SYSTEM, {"free_hint"})):
        fields = {f for _, f, _, _ in string.Formatter().parse(tpl)
                  if f is not None}
        assert fields <= allowed, f"unescaped brace field(s): {fields - allowed}"


def test_planner_prompt_defines_good_before_building():
    from brain import planner as planner_mod
    s = planner_mod._SYSTEM
    assert "KNOWLEDGE NEEDS" in s
    assert "REVISION MODE" in s
    # Generality: the new sections teach rules, not a product. Format-category
    # enumerations are fine; named trial artifacts are not.
    for residue in ("powerpoint", "slide deck", "board pack", "helioroute"):
        assert residue not in s.lower(), f"trial residue {residue!r} in planner prompt"


# --- session revision memory -------------------------------------------------------- #
def test_session_context_reaches_planner_and_revision_is_traced(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()

    async def runner(_t, name, prompt, options):
        return "revised output"

    planner = StubPlanner(
        [StageSpec(id="fixit", pattern="single", role="F", goal="apply the feedback")],
        revision={"of_run": "prev-42", "feedback": "shorten section two",
                  "preserve": "the structure the user accepted"})
    orch, bb = _orch(tmp_path, runner, planner, "rev1", tracer)
    ctx = ("previous run id: prev-42\nprevious objective: make it\n"
           "previous result: succeeded\nprevious deliverable (payload head):\n"
           "---\nthe long draft\n---")
    try:
        asyncio.run(orch.run("shorten section two", session_context=ctx))
        stored = bb.get("plan.revision")
    finally:
        bb.close()
    # The planner SAW the session context (feedback recognizable as feedback).
    assert planner.session_contexts and "prev-42" in planner.session_contexts[0]
    # The revision judgment is auditable state.
    assert stored == {"of_run": "prev-42", "feedback": "shorten section two",
                      "preserve": "the structure the user accepted"}
    assert "plan.revision" in tracer.kinds()
# --- P2: perceptual review ----------------------------------------------------------- #
def test_artifact_page_check_measures_rendered_pages():
    checks = criteria.artifact_page_check(
        ["The deck contains exactly 10 slides.", "at least 2 pages", "be clear"],
        page_count=11)
    by = {(c.unit, c.op): c for c in checks}
    assert not by[("slide", "exact")].ok and by[("slide", "exact")].gating
    assert by[("slide", "exact")].measured == 11
    assert by[("page", "min")].ok
    # Non-slide/page units are ignored here (measured on the payload instead).
    assert all(c.unit in ("slide", "page") for c in checks)
    assert criteria.artifact_page_check(["exactly 10 slides"], 10)[0].ok


def test_perceivable_artifacts_classified_by_declared_role(tmp_path):
    """Extension alone must not decide: a fetched page.html is retained
    EVIDENCE (role observation), not an audience artifact (role artifact)."""
    async def runner(_t, name, prompt, options):
        return "x"

    orch, bb = _orch(tmp_path, runner, StubPlanner([]), "cls1")
    ws = tmp_path / "runs" / "cls1" / "workspace"
    _produce(tmp_path, "cls1", "build_doc", "final.odt", role="artifact")
    _produce(tmp_path, "cls1", "chart_gen", "trend.png", role="artifact")
    _produce(tmp_path, "cls1", "fetch_page", "page.html", role="observation")
    _produce(tmp_path, "cls1", _RENDER_NEED_KEY, "p1.png", role="artifact")
    try:
        renderable, images = orch._perceivable_artifacts(ws)
    finally:
        bb.close()
    assert renderable == ["build_doc/final.odt"]
    assert images == ["chart_gen/trend.png"]      # render output itself excluded


def test_rendered_page_count_is_the_latest_render(tmp_path):
    async def runner(_t, name, prompt, options):
        return "x"

    orch, bb = _orch(tmp_path, runner, StubPlanner([]), "cnt1")
    ws = tmp_path / "runs" / "cnt1" / "workspace"
    ws.mkdir(parents=True)
    try:
        assert orch._rendered_page_count(ws) is None
        evidence.record(ws.parent, _RENDER_NEED_KEY,
                        [f"{_RENDER_NEED_KEY}/p{i}.png" for i in range(3)],
                        [], "complete", inputs=["doc.odt"], role="derived")
        assert orch._rendered_page_count(ws) == 3
        # A later render (after a fix) supersedes — stale page images from the
        # earlier render cannot skew the count.
        evidence.record(ws.parent, _RENDER_NEED_KEY,
                        [f"{_RENDER_NEED_KEY}/q{i}.png" for i in range(2)],
                        [], "complete", inputs=["doc.odt"], role="derived")
        assert orch._rendered_page_count(ws) == 2
    finally:
        bb.close()


def test_review_renders_artifact_and_page_count_gates_approval(tmp_path, monkeypatch):
    """The V0120 shape: an 11-page artifact against an explicit 10 shipped
    because counts were only ever measured on prose. With a produced artifact:
    the review stage gains the render capability (seeded as an ordinary
    engine-level need), the reviewer is told to LOOK at every page, and the
    RENDERED page count is authoritative — voiding approval until the fix."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    prompts: dict[str, str] = {}

    async def runner(_t, name, prompt, options):
        prompts[name] = prompt
        if name == "build":
            _produce(tmp_path, "percep1", "make_deck", "deck.odp", role="artifact")
            return "```deliverable\nDeck built as asked.\n```"
        if name == "build.fix":
            _produce(tmp_path, "percep1", "make_deck", "deck.odp", role="artifact",
                     content=b"fixed bytes")
            return "```deliverable\nDeck rebuilt with two slides.\n```"
        if name == "check.r1":
            # Reviewer rendered the artifact: THREE pages against 'exactly 2'.
            evidence.record(tmp_path / "runs" / "percep1", _RENDER_NEED_KEY,
                            [f"{_RENDER_NEED_KEY}/p{i}.png" for i in range(3)],
                            [], "complete", inputs=["make_deck/deck.odp"],
                            role="derived")
            return "VERDICT: approved\nlooks fine"
        if name == "check.r2":
            evidence.record(tmp_path / "runs" / "percep1", _RENDER_NEED_KEY,
                            [f"{_RENDER_NEED_KEY}/q{i}.png" for i in range(2)],
                            [], "complete", inputs=["make_deck/deck.odp"],
                            role="derived")
            return "VERDICT: approved\nnow it matches"
        raise AssertionError(name)

    planner = StubPlanner(
        [StageSpec(id="build", pattern="single", role="B", goal="build the deck"),
         StageSpec(id="check", pattern="review", role="C", goal="verify it",
                   depends_on=["build"], rounds=2)],
        success_criteria=["exactly 2 slides"])
    orch, bb = _orch(tmp_path, runner, planner, "percep1", tracer)
    try:
        asyncio.run(orch.run("build a two-slide overview of anything"))
    finally:
        bb.close()
    # The render need was seeded engine-side and the reviewer was pointed at
    # the artifact; acquisition degraded gracefully (no resolver here) instead
    # of killing the review.
    assert _RENDER_NEED_KEY in orch._cap_needs
    assert "review.capability_degraded" in tracer.kinds()
    assert "PERCEIVABLE ARTIFACTS" in prompts["check.r1"]
    assert "make_deck/deck.odp" in prompts["check.r1"]
    # Round 1: rendered 3 pages vs 'exactly 2' -> approval voided, fix demanded.
    voided = [e for e in tracer.events if e[0] == "review.approval.voided"]
    assert voided and "RENDERED artifact" in voided[0][3]["reason"]
    assert "ENGINE GATE" in prompts["build.fix"]
    # Round 2: rendered 2 pages -> approval stands; terminal measurement agrees
    # (the payload prose never says '---', yet no slide violation is reported:
    # the render is authoritative for slide counts).
    assert orch._review_approved
    assert orch.classification == "succeeded"
    assert orch._format_violations == []
    # The reviewer sees the engine's render measurement as a fact line.
    assert "ARTIFACT RENDER" in prompts["check.r2"]


def test_render_contract_is_instance_neutral():
    from brain.orchestrator import _RENDER_CONTRACT
    low = _RENDER_CONTRACT.lower()
    assert "page" in low and "render" in low
    for residue in ("powerpoint", "board", "quarter", "helioroute"):
        assert residue not in low
    # The format set is a modality category list, engine-level.
    assert ".pdf" in _RENDERABLE_EXTS and ".html" in _RENDERABLE_EXTS


# --- P3: self-evaluation before handoff ----------------------------------------------- #
# --- P4: hardening -------------------------------------------------------------------- #
def test_sample_file_not_found_lists_real_workspace_files(tmp_path):
    """The authoring loop must be able to CORRECT a bad file reference: the
    error now enumerates what actually exists instead of leaving the developer
    to guess paths blind (observed: three identical guesses, then death)."""
    from brain.models import Capability

    ws = tmp_path / "ws"
    (ws / "input").mkdir(parents=True)
    (ws / "input" / "archive.zip").write_bytes(b"PK\x03\x04")
    (ws / "prior").mkdir()
    (ws / "prior" / "old.bin").write_bytes(b"x")

    class Mgr:
        def build_image(self, tag, ctx):
            pass

        def run_ephemeral(self, *a, **kw):
            return RunResult(container_id="c", exit_code=0, stdout="{}",
                             stderr="", timed_out=False, output_files={})

    cap = Capability(name="t", description="d", image_tag="i:1", command=["x"],
                     input_schema={"type": "object",
                                   "properties": {"files": {"type": "array"}}},
                     env_map={}, network="none")
    ok, detail, _ = acquisition._generic_verify(
        cap, {"files": ["input/guessed_name.zip"]}, {}, Mgr(), workspace=ws)
    assert not ok and "SAMPLE FILE NOT FOUND" in detail
    assert "input/archive.zip" in detail          # the real file is offered
    assert "prior/old.bin" not in detail          # prior-run seeds are not inputs


def test_review_capability_failure_degrades_not_dies(tmp_path, monkeypatch):
    """A review whose ONE tool cannot be built must still review with what it
    has (observed: AcquisitionError killed the review stage wholesale and the
    draft shipped unverified with no substantive critique)."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    prompts: dict[str, str] = {}

    async def runner(_t, name, prompt, options):
        prompts[name] = prompt
        if name.startswith("check.r"):
            return "VERDICT: approved"
        return "```deliverable\nthe work\n```"

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write"),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], capabilities=["special_reader"])])
    orch, bb = _orch(tmp_path, runner, planner, "deg1", tracer)
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    # No resolver here -> acquiring 'special_reader' fails; the review ran
    # anyway, was TOLD what is missing, and the run classified normally.
    degraded = [e for e in tracer.events if e[0] == "review.capability_degraded"]
    assert degraded and degraded[0][1] == "special_reader"
    assert "could NOT be built" in prompts["check.r1"]
    assert "special_reader" in prompts["check.r1"]
    assert orch._review_approved
    assert orch.classification == "succeeded"
