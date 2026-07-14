"""v0.14 — the transport-safety + anchored-deliverable release (from the two
board-pack revision runs): the SDK's 1 MiB frame limit killed three sessions
including a final review round; a crashed round aborted the whole review and
shipped UNVERIFIED holding an unapplied defect list; an extension made the
INSPECTOR the deliverable owner and its report shipped as DELIVERABLE.md; a
broken tool ended as a blocker note and silently degraded output while
repair_capability sat unused; seeded prior-run files arrived lineage-naked
(target_evidence: 0 on a revision); research pages were scanned as artifact
defects; and a stale render certified a deck that no longer existed."""
from __future__ import annotations

import asyncio
import io
import json

from brain import config, evidence, workflow
from brain.capabilities.runtime import (_encoded_len, _fit_image, fit_result_text,
                                        image_blocks)
from brain.memory.blackboard import Blackboard
from brain.models import Capability, StageSpec
from brain.observability import NullTracer
from brain.orchestrator import _RENDER_NEED_KEY, Orchestrator, _seed_prior_workspace
from brain.orchestrator import artifact_issues
from brain.planner import StubPlanner
from brain.team import TeamChannel


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


def _noise_png(w=1600, h=1000):
    """A real, poorly-compressible PNG (random noise) for downscale tests."""
    import os

    from PIL import Image
    img = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _produce(tmp_path, session, cap, name, role, content=b"bytes", refs=("args",),
             inputs=()):
    d = tmp_path / "runs" / session / "workspace" / cap
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(content)
    evidence.record(tmp_path / "runs" / session, cap, [f"{cap}/{name}"],
                    list(refs), "complete", inputs=list(inputs), role=role)
    return f"{cap}/{name}"


# --- transport-safe results --------------------------------------------------------- #
def test_fit_result_text_keeps_head_and_tail():
    text = "H" * 50000 + "MIDDLE" + "T" * 50000
    out = fit_result_text(text, 20000)
    assert len(out) < 22000
    assert out.startswith("H") and out.endswith("T" * 10)
    assert "exceeded the transport-safe size" in out
    assert fit_result_text("short", 20000) == "short"


def test_fit_image_downscales_to_encoded_budget():
    data = _noise_png()
    assert _encoded_len(len(data)) > 200_000          # genuinely oversize
    fitted = _fit_image(data, "image/png", 200_000)
    assert fitted is not None
    raw, mime = fitted
    assert _encoded_len(len(raw)) <= 200_000
    assert mime == "image/jpeg"
    # Under-budget images pass through untouched.
    small = _noise_png(64, 64)
    assert _fit_image(small, "image/png", 1_000_000) == (small, "image/png")


def test_image_blocks_respect_cumulative_budget(tmp_path):
    ws = tmp_path / "ws"
    (ws / "cap").mkdir(parents=True)
    for i in range(3):
        (ws / "cap" / f"p{i}.png").write_bytes(_noise_png())
    budget = 400_000
    blocks = image_blocks([f"cap/p{i}.png" for i in range(3)], ws,
                          max_images=3, max_bytes=8 * 1024 * 1024,
                          budget_bytes=budget)
    assert blocks                                       # perception survives…
    assert sum(len(b["data"]) for b in blocks) <= budget   # …under the frame limit


def test_read_file_never_returns_image_bytes_as_text(tmp_path):
    async def runner(_t, name, prompt, options):
        return "x"

    orch, bb = _orch(tmp_path, runner, StubPlanner([]), "img1")
    ws = tmp_path / "runs" / "img1" / "workspace"
    (ws / "cap").mkdir(parents=True)
    # Oversize AND unopenable: cannot be fitted -> honest note, never a text read.
    (ws / "cap" / "broken.png").write_bytes(b"\x89PNG" + b"\x00" * 900_000)
    try:
        out = orch._read_workspace(ws, "cap/broken.png")
    finally:
        bb.close()
    text = out["content"][0]["text"]
    assert "could not be displayed" in text
    assert "\x00" not in text


# --- review-round resilience -------------------------------------------------------- #
def test_crashed_review_round_degrades_to_next_round(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    prompts: dict[str, list] = {}

    async def runner(_t, name, prompt, options):
        prompts.setdefault(name, []).append(prompt)
        if name == "check.r1":
            raise RuntimeError("JSON message exceeded maximum buffer size of 1048576")
        if name == "check.r2":
            return "VERDICT: approved"
        return "```deliverable\nthe work\n```"

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write"),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], rounds=2)])
    orch, bb = _orch(tmp_path, runner, planner, "crash1", tracer)
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    assert "review.round_failed" in tracer.kinds()
    assert "CRASHED mid-session" in prompts["check.r2"][0]
    assert orch._review_approved
    assert orch.classification == "succeeded"
    rec = (tmp_path / "runs" / "crash1" / "outputs" / "check.md").read_text()
    assert "errored" in rec and "approved" in rec


def test_crashed_fix_round_leaves_draft_standing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()

    async def runner(_t, name, prompt, options):
        if name == "draft.fix":
            raise RuntimeError("fix agent died")
        if name == "check.r1":
            return "VERDICT: revise\n1. tighten it."
        if name == "check.r2":
            assert "fix round after round 1 CRASHED" in prompt
            return "VERDICT: approved"
        return "```deliverable\nthe work\n```"

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write"),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], rounds=2)])
    orch, bb = _orch(tmp_path, runner, planner, "crash2", tracer)
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    assert "review.revision_failed" in tracer.kinds()
    assert orch.classification == "succeeded"


def test_fix_rounds_carry_lessons_forward(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    prompts: dict[str, list] = {}
    verdicts = iter(["VERDICT: revise\n1. fix count.",
                     "VERDICT: revise\n2. fix labels.",
                     "VERDICT: approved"])

    async def runner(_t, name, prompt, options):
        prompts.setdefault(name, []).append(prompt)
        if name.startswith("check.r"):
            return next(verdicts)
        if name == "draft.fix":
            return ("LESSON: the generator adds a cover slide automatically.\n"
                    "```deliverable\nthe work v2\n```")
        return "```deliverable\nthe work\n```"

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write"),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], rounds=3)])
    orch, bb = _orch(tmp_path, runner, planner, "carry1")
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    # Second fix round sees the first fix round's operational lesson.
    assert len(prompts["draft.fix"]) == 2
    assert "EARLIER FIX ROUNDS' WORKING NOTES" in prompts["draft.fix"][1]
    assert "adds a cover slide automatically" in prompts["draft.fix"][1]
    assert "adds a cover slide" not in prompts["draft.fix"][0]


# --- deliverable anchoring ----------------------------------------------------------- #
def test_deliverable_marking_overrides_graph_heuristic():
    stages = [StageSpec(id="produce", pattern="single", role="P", goal="g",
                        deliverable=True),
              StageSpec(id="inspect", pattern="single", role="I", goal="g",
                        depends_on=["produce"])]
    assert workflow.deliverable_stage_ids(stages) == {"produce"}
    # Unmarked -> heuristic unchanged (terminal stage).
    stages[0].deliverable = False
    assert workflow.deliverable_stage_ids(stages) == {"inspect"}
    # normalize_stages parses the flag; review stages can never carry it.
    parsed, _ = workflow.normalize_stages(
        [{"id": "a", "pattern": "single", "role": "r", "goal": "g", "deliverable": True},
         {"id": "v", "pattern": "review", "role": "r", "goal": "g", "deliverable": True,
          "depends_on": ["a"]}])
    assert parsed[0].deliverable and not parsed[1].deliverable


def test_deliverable_ships_from_owner_not_terminal_stage(tmp_path, monkeypatch):
    """The inspection-report-as-deliverable failure: the terminal stage is an
    inspector; the marked producer's payload must ship."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()

    async def runner(_t, name, prompt, options):
        if name == "produce":
            return "notes\n```deliverable\nTHE ARTIFACT\n```"
        return "INSPECTION REPORT: 3 defects found"

    planner = StubPlanner(
        [StageSpec(id="produce", pattern="single", role="P", goal="make it",
                   deliverable=True),
         StageSpec(id="inspect", pattern="single", role="I", goal="inspect it",
                   depends_on=["produce"])])
    orch, bb = _orch(tmp_path, runner, planner, "anchor1", tracer)
    try:
        answer = asyncio.run(orch.run("make the artifact"))
    finally:
        bb.close()
    pure = (tmp_path / "runs" / "anchor1" / "outputs" / "DELIVERABLE.md").read_text()
    assert pure == "THE ARTIFACT"
    assert "deliverable.source" in tracer.kinds()
    assert "INSPECTION REPORT" in answer          # the terminal narrative still answers


def test_retarget_keeps_marked_deliverable_as_review_draft(tmp_path):
    async def runner(_t, name, prompt, options):
        return "x"

    orch, bb = _orch(tmp_path, runner, StubPlanner([]), "ret1",
                     RecordingTracer())
    stages = [StageSpec(id="produce", pattern="single", role="P", goal="g",
                        deliverable=True),
              StageSpec(id="verify", pattern="review", role="V", goal="g",
                        depends_on=["produce"])]
    new = [StageSpec(id="render", pattern="single", role="R", goal="g"),
           StageSpec(id="inspect", pattern="single", role="I", goal="g")]
    stages[1:1] = new
    try:
        orch._retarget_pending_review(stages, 0, new)
    finally:
        bb.close()
    review = stages[-1]
    assert review.depends_on[0] == "produce"          # owner stays the draft
    assert set(review.depends_on[1:]) == {"render", "inspect"}


# --- agent-reported tool defects route to repair -------------------------------------- #
def test_tool_defect_channel_records_and_guides(tmp_path):
    bb = Blackboard(tmp_path / "b.db", "defect0")
    tracer = RecordingTracer()
    ch = TeamChannel(agent_id="builder", completed_stages=[], ask_fn=None,
                     tracer=tracer, blackboard=bb)
    try:
        text = ch.tool_defect("gen_tool", "AttributeError: no chart_area", "3 retries")
        stored = bb.get("tool.defects")
    finally:
        bb.close()
    assert "engine will attempt a repair" in text
    assert stored and stored[0]["tool"] == "gen_tool"
    assert "team.tool_defect" in tracer.kinds()


def test_reported_defect_repairs_tool_and_reruns_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    repaired: list[str] = []
    cap = Capability(name="gen_tool", description="d", image_tag="i:1",
                     command=["x"], input_schema={}, env_map={})

    async def fake_repair(broken, reason, **kw):
        repaired.append(reason)
        return cap

    import brain.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "acquire_repair", fake_repair)

    async def runner(_t, name, prompt, options):
        if name == "build":
            bb.put("tool.defects", [{"agent": "build", "tool": "gen_tool",
                                     "error": "AttributeError: chart_area"}])
            return "degraded fallback (reported the defect)"
        if name == "build.retool":
            assert "was REPAIRED" in prompt
            return "proper output using the repaired tool"
        raise AssertionError(name)

    planner = StubPlanner([StageSpec(id="build", pattern="single", role="B",
                                     goal="build it")])
    orch, bb = _orch(tmp_path, runner, planner, "defect1", tracer)
    orch.manager, orch.registry = object(), object()   # repair path prerequisites

    async def fake_acquire(spec):
        return {"gen_tool": cap}

    orch._acquire_caps = fake_acquire
    try:
        answer = asyncio.run(orch.run("build the thing"))
    finally:
        bb.close()
    assert repaired and "AttributeError" in repaired[0]
    assert "stage.rerun_after_repair" in tracer.kinds()
    assert "proper output" in answer
    assert orch._tool_repairs == 1


def test_defect_repair_budget_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    cap = Capability(name="gen_tool", description="d", image_tag="i:1",
                     command=["x"], input_schema={}, env_map={})

    async def runner(_t, name, prompt, options):
        if name == "build":
            bb.put("tool.defects", [{"agent": "build", "tool": "gen_tool",
                                     "error": "still broken"}])
            return "fallback"
        raise AssertionError(name)

    planner = StubPlanner([StageSpec(id="build", pattern="single", role="B",
                                     goal="build it")])
    orch, bb = _orch(tmp_path, runner, planner, "defect2", tracer)
    orch.manager, orch.registry = object(), object()

    async def fake_acquire(spec):
        return {"gen_tool": cap}

    orch._acquire_caps = fake_acquire
    monkeypatch.setattr(config, "TOOL_DEFECT_REPAIRS_PER_RUN", 0)
    try:
        asyncio.run(orch.run("build the thing"))
    finally:
        bb.close()
    assert "capability.defect_unrepaired" in tracer.kinds()
    assert "stage.rerun_after_repair" not in tracer.kinds()


# --- seeded lineage inheritance -------------------------------------------------------- #
def test_seeded_prior_run_carries_lineage_forward(tmp_path):
    runs = tmp_path / "runs"
    seed_ws = runs / "seedX" / "workspace"
    (seed_ws / "input").mkdir(parents=True)
    (seed_ws / "input" / "src.pdf").write_bytes(b"%PDF prior input")
    (seed_ws / "extract").mkdir()
    (seed_ws / "extract" / "notes.txt").write_text("extracted content")
    evidence.record(runs / "seedX", "extract", ["extract/notes.txt"],
                    ["https://example.test/doc"], "complete",
                    inputs=["input/src.pdf"], role="observation")
    (runs / "seedX" / "locators.json").write_text(
        json.dumps(["https://example.test/doc"]))

    cur = runs / "cur"
    (cur / "workspace").mkdir(parents=True)
    copied = _seed_prior_workspace(runs, "cur", "seedX")
    assert any("notes.txt" in c for c in copied)
    n = evidence.inherit_records(cur, runs / "seedX", "seedX")
    assert n >= 2                                     # tool record + input record

    # No locators in the NEW run — tiers must come from inheritance.
    entries = evidence.ledger(cur / "workspace", (), [])
    tiers = {e.file: e.tier for e in entries}
    assert tiers["prior/seedX/input/src.pdf"] == "target"
    assert tiers["prior/seedX/extract/notes.txt"] in ("target", "derived")
    assert evidence.target_lineage(entries)

    # And lineage PROPAGATES: consuming an inherited target yields derived.
    (cur / "workspace" / "rebuild").mkdir()
    (cur / "workspace" / "rebuild" / "out.txt").write_text("rebuilt")
    evidence.record(cur, "rebuild", ["rebuild/out.txt"], [], "complete",
                    inputs=["prior/seedX/extract/notes.txt"], role="artifact")
    entries = evidence.ledger(cur / "workspace", (), [])
    assert {e.file: e.tier for e in entries}["rebuild/out.txt"] == "derived"


# --- role-scoped artifact hygiene ------------------------------------------------------ #
def test_artifact_hygiene_skips_observation_role_files(tmp_path):
    session = "hyg1"
    bad_html = "<html><title>docs/page.html</title><body>truncated mid-sent</body></html>"
    _produce(tmp_path, session, "fetch_page", "raw.html", role="observation",
             content=bad_html.encode())
    _produce(tmp_path, session, "site_gen", "index.html", role="artifact",
             content=bad_html.encode())
    ws = tmp_path / "runs" / session / "workspace"
    issues = artifact_issues(ws)
    assert any("site_gen/index.html" in i for i in issues)
    assert not any("fetch_page/raw.html" in i for i in issues)


# --- perception freshness --------------------------------------------------------------- #
def test_render_detected_by_shape_and_staleness_by_order(tmp_path):
    async def runner(_t, name, prompt, options):
        return "x"

    orch, bb = _orch(tmp_path, runner, StubPlanner([]), "fresh1")
    session = "fresh1"
    ws = tmp_path / "runs" / session / "workspace"
    try:
        _produce(tmp_path, session, "make_doc", "doc.odt", role="artifact")
        # A custom-named renderer counts by SHAPE (images from a renderable).
        d = ws / "my_custom_renderer"
        d.mkdir(parents=True)
        for i in range(3):
            (d / f"p{i}.png").write_bytes(b"png")
        evidence.record(ws.parent, "my_custom_renderer",
                        [f"my_custom_renderer/p{i}.png" for i in range(3)],
                        [], "complete", inputs=["make_doc/doc.odt"], role="derived")
        assert orch._rendered_page_count(ws) == 3
        assert orch._render_is_stale(ws) is False
        # The artifact is regenerated AFTER the render -> stale.
        _produce(tmp_path, session, "make_doc", "doc.odt", role="artifact",
                 content=b"v2")
        assert orch._render_is_stale(ws) is True
        # Stale render measures nothing (no false certification either way).
        orch._criteria = ["exactly 2 pages"]
        assert all(c.unit not in ("slide", "page")
                   for c in orch._measured_checks("payload", ws))
    finally:
        bb.close()


def test_stale_render_voids_approval_until_rerendered(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    session = "fresh2"

    async def runner(_t, name, prompt, options):
        if name == "build":
            _produce(tmp_path, session, "make_doc", "doc.odp", role="artifact")
            evidence.record(tmp_path / "runs" / session, _RENDER_NEED_KEY,
                            [f"{_RENDER_NEED_KEY}/p1.png", f"{_RENDER_NEED_KEY}/p2.png"],
                            [], "complete", inputs=["make_doc/doc.odp"], role="derived")
            # …then the artifact is REGENERATED after that render.
            _produce(tmp_path, session, "make_doc", "doc.odp", role="artifact",
                     content=b"v2")
            return "```deliverable\nDeck built.\n```"
        if name == "build.fix":
            return "```deliverable\nDeck rebuilt.\n```"
        if name == "check.r1":
            return "VERDICT: approved\nlooks fine"     # over STALE perception
        if name == "check.r2":
            evidence.record(tmp_path / "runs" / session, _RENDER_NEED_KEY,
                            [f"{_RENDER_NEED_KEY}/q1.png", f"{_RENDER_NEED_KEY}/q2.png"],
                            [], "complete", inputs=["make_doc/doc.odp"], role="derived")
            return "VERDICT: approved\nfresh render checked"
        raise AssertionError(name)

    planner = StubPlanner(
        [StageSpec(id="build", pattern="single", role="B", goal="build"),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["build"], rounds=2)],
        success_criteria=["exactly 2 slides"])
    orch, bb = _orch(tmp_path, runner, planner, session, tracer)
    try:
        asyncio.run(orch.run("build a two-slide overview"))
    finally:
        bb.close()
    voided = [e for e in tracer.events if e[0] == "review.approval.voided"]
    assert voided and "stale perception" in voided[0][3]["reason"]
    assert orch._review_approved                       # round 2, over fresh render
    assert orch.classification == "succeeded"


def test_unperceived_renderable_artifact_voids_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    session = "fresh3"

    async def runner(_t, name, prompt, options):
        if name == "build":
            _produce(tmp_path, session, "make_doc", "doc.odp", role="artifact")
            return "```deliverable\nDeck built.\n```"
        if name == "check.r1":
            return "VERDICT: approved\nnever rendered, approving anyway"
        if name == "build.fix":
            return "```deliverable\nDeck rebuilt.\n```"
        if name == "check.r2":
            evidence.record(tmp_path / "runs" / session, _RENDER_NEED_KEY,
                            [f"{_RENDER_NEED_KEY}/p1.png"],
                            [], "complete", inputs=["make_doc/doc.odp"], role="derived")
            return "VERDICT: approved\nlooked at it now"
        raise AssertionError(name)

    render_cap = Capability(name=_RENDER_NEED_KEY, description="renders pages",
                            image_tag="i:1", command=["x"],
                            input_schema={"type": "object", "properties": {}},
                            env_map={})

    async def fake_best_effort(spec):
        return {_RENDER_NEED_KEY: render_cap}, ""      # render tool IS available

    planner = StubPlanner(
        [StageSpec(id="build", pattern="single", role="B", goal="build"),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["build"], rounds=2)])
    orch, bb = _orch(tmp_path, runner, planner, session, tracer)
    orch._acquire_caps_best_effort = fake_best_effort
    try:
        asyncio.run(orch.run("build a deck"))
    finally:
        bb.close()
    voided = [e for e in tracer.events if e[0] == "review.approval.voided"]
    assert voided and "unperceived artifact" in voided[0][3]["reason"]
    assert orch._review_approved
    assert orch.classification == "succeeded"
