"""v0.16 — the see-your-own-file-state + actionable-review release (from a
5-task external campaign): a worker declared its workspace EMPTY while every
file it needed sat under a seeded `prior/` directory that the read_file miss
message silently excluded; another worker read a first-40-of-129 truncated
listing as proof that the files beyond it did not exist and reported a false
coverage gap the review then rightly flagged; three planner-written reviews
carried `rounds: 1` (the planner contract read "fix cycles", the executor
counted verdict rounds) so every first-round "revise" verdict exhausted the
review with its critique never applied; and supervisors ordered `redo_stage`
on review stages four times across two cycles with the directive silently
dropped."""
from __future__ import annotations

import asyncio

from brain import config, factory, workflow
from brain.memory.blackboard import Blackboard
from brain.models import AgentSpec, StageSpec
from brain.observability import NullTracer
from brain.orchestrator import (Orchestrator, _missing_file_error,
                                _workspace_files, _workspace_rollup)
from brain.planner import StubPlanner
from brain.supervisor import Supervisor


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
    return o


# --- process: a review must be able to ACT on its own first verdict --------------- #
def test_explicit_one_round_review_is_floored_to_two():
    """The planner contract read `rounds` as fix cycles; the executor counts
    verdict rounds — an explicit `rounds: 1` bought zero fix capacity and
    turned every first-round revise into instant exhaustion."""
    stages, _ = workflow.normalize_stages([
        {"id": "draft", "pattern": "single", "goal": "write"},
        {"id": "check", "pattern": "review", "goal": "verify",
         "depends_on": ["draft"], "rounds": 1},
    ])
    assert next(s for s in stages if s.id == "check").rounds == 2


def test_review_floor_respects_the_configured_max(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_MAX_ROUNDS", 1)
    stages, _ = workflow.normalize_stages([
        {"id": "draft", "pattern": "single", "goal": "write"},
        {"id": "check", "pattern": "review", "goal": "verify",
         "depends_on": ["draft"], "rounds": 1},
    ])
    assert next(s for s in stages if s.id == "check").rounds == 1


def test_non_review_stages_keep_their_single_round():
    stages, _ = workflow.normalize_stages([
        {"id": "work", "pattern": "single", "goal": "do", "rounds": 1},
    ])
    assert stages[0].rounds == 1


# --- perception: the miss message must correct the agent, never mislead it -------- #
def test_miss_message_shows_prior_seeds_not_an_empty_workspace(tmp_path):
    """The campaign shape: every file the worker needed sat under `prior/`,
    and the old listing excluded `prior/` — the engine told the agent its
    workspace was empty while the workspace held everything."""
    ws = tmp_path / "ws"
    (ws / "prior/run-0/input").mkdir(parents=True)
    (ws / "prior/run-0/input/brief.md").write_text("the source")
    (ws / "prior/run-0/outputs").mkdir(parents=True)
    (ws / "prior/run-0/outputs/DELIVERABLE.md").write_text("the prior artifact")
    msg = _missing_file_error(ws, "input/brief.md")
    assert "prior/run-0/input/brief.md" in msg          # exact-name nearest match
    assert "2 file(s)" in msg
    assert "NOT evidence" in msg                        # anti-absence framing
    assert "empty" not in msg.lower()


def test_miss_message_reveals_the_far_end_of_long_sequences(tmp_path):
    """A first-N-sorted cut hides exactly the far end an agent asks about; the
    rollup shows count plus FIRST and LAST names so a numbered sequence
    reveals its full range."""
    ws = tmp_path / "ws"
    d = ws / "produced"
    d.mkdir(parents=True)
    for i in range(128):
        (d / f"item_{i:04d}.jpg").write_bytes(b"x")
    msg = _missing_file_error(ws, "elsewhere/item_0075.jpg")
    assert "produced/item_0075.jpg" in msg              # it EXISTS, say where
    assert "item_0000" in msg and "item_0127" in msg    # first … last, not first-40
    assert "128 files" in msg


def test_rollup_groups_by_directory_with_first_and_last():
    lines = _workspace_rollup([
        "a/f_001.txt", "a/f_002.txt", "a/f_003.txt", "a/f_004.txt", "root.md"])
    joined = "\n".join(lines)
    assert "- a/: 4 files, f_001.txt … f_004.txt" in joined
    assert "(workspace root): root.md" in joined


def test_workspace_files_include_prior(tmp_path):
    ws = tmp_path / "ws"
    (ws / "prior/run-0").mkdir(parents=True)
    (ws / "prior/run-0/x.txt").write_text("x")
    (ws / "y.txt").write_text("y")
    assert _workspace_files(ws) == ["prior/run-0/x.txt", "y.txt"]


def test_read_workspace_miss_is_corrective(tmp_path):
    ws = tmp_path / "ws"
    (ws / "prior/run-0").mkdir(parents=True)
    (ws / "prior/run-0/notes.md").write_text("n")
    res = _bare_orch()._read_workspace(ws, "notes.md")
    assert res.get("is_error")
    text = res["content"][0]["text"]
    assert "prior/run-0/notes.md" in text and "list_files" in text


# --- perception: deterministic, paged enumeration ---------------------------------- #
def test_list_workspace_pages_and_labels_truncation(tmp_path):
    ws = tmp_path / "ws"
    d = ws / "bulk"
    d.mkdir(parents=True)
    for i in range(250):
        (d / f"f_{i:03d}.txt").write_text("x")
    o = _bare_orch()
    first = o._list_workspace(ws, "", 0)["content"][0]["text"]
    assert "250 file(s)" in first
    assert "TRUNCATED" in first and "offset=200" in first
    assert "f_199.txt" in first and "f_200.txt" not in first
    second = o._list_workspace(ws, "", 200)["content"][0]["text"]
    assert "f_249.txt" in second and "TRUNCATED" not in second


def test_list_workspace_globs_full_path_and_basename(tmp_path):
    ws = tmp_path / "ws"
    (ws / "prior/run-0").mkdir(parents=True)
    (ws / "prior/run-0/chart.png").write_bytes(b"p")
    (ws / "notes").mkdir(parents=True)
    (ws / "notes/summary.txt").write_text("s")
    o = _bare_orch()
    by_ext = o._list_workspace(ws, "*.png")["content"][0]["text"]
    assert "prior/run-0/chart.png" in by_ext and "summary.txt" not in by_ext
    by_dir = o._list_workspace(ws, "prior/*")["content"][0]["text"]
    assert "prior/run-0/chart.png" in by_dir and "summary.txt" not in by_dir
    none = o._list_workspace(ws, "*.mp4")["content"][0]["text"]
    assert "no files match" in none


# --- perception: the run's file state is a fact handed to every agent -------------- #
def test_agent_user_prompt_carries_the_inventory():
    spec = AgentSpec(id="a", role="R", goal="G", capabilities=[], depends_on=[])
    p = factory.agent_user_prompt("obj", spec, {}, history="earlier run notes",
                                  workspace_inventory="3 file(s): x")
    assert "WORKSPACE FILES (engine-enumerated): 3 file(s): x" in p
    # The prior-work hint points at the inventory instead of speculating. This
    # asserts the wording that EXISTS; the previous negative guarded a phrase
    # present nowhere in the codebase, so it could never fail.
    assert "trust the WORKSPACE FILES inventory" in p
    assert "never a remembered or assumed layout" in p
    assert factory.agent_user_prompt("obj", spec, {}).count("WORKSPACE FILES") == 0


def test_sessions_receive_inventory_and_the_listing_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    seen: dict = {}

    async def runner(_t, name, prompt, options):
        seen[name] = (prompt, options)
        return "```deliverable\nok\n```"

    planner = StubPlanner([StageSpec(id="work", pattern="single", role="W", goal="do")])
    seeded = tmp_path / "runs" / "inv1" / "workspace" / "prior" / "run-0" / "outputs"
    seeded.mkdir(parents=True)
    (seeded / "DELIVERABLE.md").write_text("the prior artifact")
    orch, bb = _orch(tmp_path, runner, planner, "inv1")
    try:
        asyncio.run(orch.run("do the thing"))
    finally:
        bb.close()
    prompt, options = seen["work"]
    assert "WORKSPACE FILES (engine-enumerated)" in prompt
    assert "prior/run-0/outputs/" in prompt
    assert "mcp__workspace__list_files" in options.allowed_tools


# --- tooling: a dropped executive directive is a visible fact ----------------------- #
def test_unhonorable_redo_stage_emits_an_event(tmp_path, monkeypatch):
    """Four runs across two cycles shipped unverified with the supervisor's
    redo order recorded and NOTHING in the trace saying it could not be
    honored — review stages register no redo, and the drop was silent."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    consults = {"n": 0}

    async def runner(_t, name, prompt, options):
        if name.startswith("check.r"):
            return "VERDICT: revise\nfix the count"
        return "```deliverable\nthe work\n```"

    async def decide(moment, digest):
        if moment != "stage_completed":
            return {"action": "proceed", "reason": "ok"}
        consults["n"] += 1
        if consults["n"] == 2:  # the consult after the review stage completes
            return {"action": "redo_stage", "guidance": "apply the critique",
                    "reason": "review verdict is revise, not approved"}
        return {"action": "proceed", "reason": "ok"}

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write"),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], rounds=1)])
    orch, bb = _orch(tmp_path, runner, planner, "redo1", tracer,
                     supervisor=Supervisor(decide_fn=decide, tracer=tracer))
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    unavailable = [(s, d) for k, s, _st, d in tracer.events
                   if k == "supervisor.redo_unavailable"]
    assert unavailable and unavailable[0][0] == "check"
    assert "review" in unavailable[0][1].get("note", "")
    assert "supervisor.redo" not in tracer.kinds()
