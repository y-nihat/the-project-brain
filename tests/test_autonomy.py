"""Autonomous tool creation + native multimodal perception (V060 follow-up).

The two behaviours the user asked for, pinned generically (no video/frame/VLM
in engine logic):

  1. A source-derived plan that skipped DIRECT observation with no legitimate
     blocker is corrected — the planner is told tool-absence is never a blocker
     and re-plans to observe the source (authoring the tool), instead of
     settling for a weaker proxy.
  2. Perceiving retained files — reading text and LOOKING AT images — is the
     agent's OWN ability: every stage agent gets an image-aware read_file tool,
     so a plan of [author an extractor] -> [agent perceives the files] runs
     without ever building a vision model.

Plus: a file dropped in the sanctioned inbox and named by path is attached like
an @mention, so a source can be provided directly.
"""
from __future__ import annotations

import asyncio

from brain import config, files as files_mod
from brain.memory.blackboard import Blackboard
from brain.models import StageSpec
from brain.observability import NullTracer
from brain.orchestrator import Orchestrator
from brain.planner import (StubPlanner, observation_is_accountable,
                           _normalize_direct_observation)


class RecordingTracer(NullTracer):
    def __init__(self):
        self.events = []

    def event(self, kind, subject, status="info", **data):
        self.events.append((kind, subject, status, data))

    def kinds(self):
        return [k for k, *_ in self.events]


# --- accountability of the direct-observation declaration ---------------------- #
def test_observation_accountability_rules():
    assert observation_is_accountable({"decision": "planned"})
    assert observation_is_accountable(
        {"decision": "infeasible", "blocker_class": "requires_credential"})
    # "no tool" is not expressible as a blocker -> not accountable.
    assert not observation_is_accountable(
        {"decision": "infeasible", "blocker_class": "no_tool_available"})
    assert not observation_is_accountable({"decision": "unstated"})
    assert not observation_is_accountable({})
    # Normalizer strips illegitimate blocker classes and defaults to unstated.
    n = _normalize_direct_observation(
        {"decision": "infeasible", "blocker_class": "no_tool_available"})
    assert n["decision"] == "infeasible" and n["blocker_class"] == ""
    assert _normalize_direct_observation("garbage")["decision"] == "unstated"


# --- the proxy-first correction ------------------------------------------------- #
class CorrectingPlanner(StubPlanner):
    """First plan is proxy-first (no direct observation); on the corrective
    re-plan it returns a plan that observes the source directly — the behaviour
    a compliant planner exhibits once told tool-absence is not a blocker."""

    def __init__(self, proxy_stages, direct_stages, direct_needs=None):
        super().__init__(proxy_stages)
        self._direct_stages = direct_stages
        self._direct_needs = direct_needs or {}

    async def plan(self, objective, history=None, corrective=None,
                   session_context=None):
        self.plan_calls += 1
        if corrective:
            self.correctives.append(corrective)
            from brain.models import Plan
            return Plan(objective=objective, stages=list(self._direct_stages),
                        capability_needs=dict(self._direct_needs),
                        direct_observation={"decision": "planned",
                                            "capability": "observe_source"})
        from brain.models import Plan
        return Plan(objective=objective, stages=list(self._stages),
                    direct_observation={})            # proxy-first, unstated


def test_proxy_first_plan_is_corrected_to_direct_observation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    bb = Blackboard(tmp_path / "b.db", "corr")

    async def runner(_t, name, prompt, options):
        if name == "observe":
            d = tmp_path / "runs" / "corr" / "workspace" / "observer"
            d.mkdir(parents=True, exist_ok=True)
            (d / "payload.txt").write_text("directly observed content")
            return "observed the source directly"
        if name.startswith("review") or name.endswith(".r1"):
            return "VERDICT: approved"
        return f"out({name})"

    planner = CorrectingPlanner(
        proxy_stages=[StageSpec(id="search", pattern="single", role="R",
                                goal="web search about the source", capabilities=["web_search"])],
        direct_stages=[StageSpec(id="observe", pattern="single", role="O",
                                 goal="observe the designated source", capabilities=["fetch_it"])])
    orch = Orchestrator(session="corr", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner, tracer=tracer)
    try:
        asyncio.run(orch.run("Explain what happens in https://example.test/v/abc"))
    finally:
        bb.close()
    # The proxy-first plan was detected and corrected exactly once.
    assert planner.plan_calls == 2
    assert planner.correctives and "NOT A BLOCKER" in planner.correctives[0]
    assert "plan.proxy_first" in tracer.kinds()
    assert "plan.corrected" in tracer.kinds()
    # The run executed the DIRECT-observation plan, not the search proxy.
    assert "observe" in orch._stages_by_id and "search" not in orch._stages_by_id


def test_accountable_plan_is_not_corrected(tmp_path, monkeypatch):
    """A plan that already observes the source directly must not be re-planned."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    bb = Blackboard(tmp_path / "b.db", "acc")

    async def runner(_t, name, prompt, options):
        if name == "obs":
            d = tmp_path / "runs" / "acc" / "workspace" / "o"
            d.mkdir(parents=True, exist_ok=True)
            (d / "p.txt").write_text("observed")
            return "observed"
        if name.startswith("review"):
            return "VERDICT: approved"
        return "x"

    planner = StubPlanner([StageSpec(id="obs", pattern="single", role="O",
                                     goal="observe the source", capabilities=["fetch_it"])],
                          direct_observation={"decision": "planned"})
    orch = Orchestrator(session="acc", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner)
    try:
        asyncio.run(orch.run("Explain what happens in https://example.test/v/abc"))
    finally:
        bb.close()
    assert planner.plan_calls == 1                     # no corrective re-plan


def test_infeasible_with_real_blocker_is_not_corrected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    bb = Blackboard(tmp_path / "b.db", "blk")

    async def runner(_t, name, prompt, options):
        if name.startswith("review"):
            return "VERDICT: revise\n1. no source evidence"
        return "proxy summary"

    planner = StubPlanner(
        [StageSpec(id="s", pattern="single", role="R", goal="g", capabilities=["web_search"])],
        direct_observation={"decision": "infeasible", "blocker_class": "requires_credential",
                            "explanation": "the source is behind a paywall/login"})
    orch = Orchestrator(session="blk", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner)
    try:
        asyncio.run(orch.run("Summarize https://paywalled.test/x"))
    finally:
        bb.close()
    assert planner.plan_calls == 1                     # a real blocker is accepted


def test_non_source_derived_plan_is_never_corrected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    bb = Blackboard(tmp_path / "b.db", "pure")

    async def runner(_t, name, prompt, options):
        return "a fine poem"

    planner = StubPlanner([StageSpec(id="w", pattern="single", role="W", goal="write")],
                          direct_observation={})
    orch = Orchestrator(session="pure", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner)
    try:
        asyncio.run(orch.run("Write me a short poem about the sea"))  # no designated source
    finally:
        bb.close()
    assert planner.plan_calls == 1


# --- multimodal perception is the agent's own ability -------------------------- #
def test_stage_agents_get_the_image_aware_read_file_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    bb = Blackboard(tmp_path / "b.db", "rf")
    seen_allowed: dict[str, list] = {}

    async def runner(_t, name, prompt, options):
        seen_allowed[name] = list(getattr(options, "allowed_tools", []) or [])
        return f"out({name})"

    planner = StubPlanner([StageSpec(id="describe", pattern="single", role="Viewer",
                                     goal="look at the frames and describe them")])
    orch = Orchestrator(session="rf", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner)
    try:
        asyncio.run(orch.run("describe the scene"))
    finally:
        bb.close()
    # A capability-free perception stage still gets read_file — perceiving
    # retained images is its own ability, no VLM capability required.
    assert "mcp__workspace__read_file" in seen_allowed["describe"]


def test_read_file_returns_images_as_images(tmp_path):
    """The read_file tool hands an image back as an image content block so the
    agent can actually SEE it — the mechanism that replaces authoring a VLM."""
    bb = Blackboard(tmp_path / "b.db", "img")
    orch = Orchestrator(session="img", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=None, resolver=None)
    ws = tmp_path / "ws"
    (ws / "frames").mkdir(parents=True)
    (ws / "frames" / "f001.png").write_bytes(b"\x89PNG fake frame bytes")
    (ws / "notes.txt").write_text("plain text note")
    try:
        img = orch._read_workspace(ws, "frames/f001.png")
        assert any(b.get("type") == "image" and b.get("data") for b in img["content"])
        txt = orch._read_workspace(ws, "notes.txt")
        assert txt["content"][0]["type"] == "text" and "plain text note" in txt["content"][0]["text"]
        assert orch._read_workspace(ws, "nope.png").get("is_error")
        # Path traversal is refused.
        assert orch._read_workspace(ws, "../../etc/passwd").get("is_error")
    finally:
        bb.close()


def test_read_file_works_with_a_real_tracer(tmp_path):
    """REGRESSION (the bug that killed the video run): read_file passed
    kind='image' to Tracer.event, colliding with its positional `kind`
    parameter — every image read returned 'got multiple values for argument
    kind', and 12 map workers retried it into the 40-session cap ($10.27, run
    blocked, zero frames ever seen). The perception path MUST be exercised with
    a REAL Tracer; NullTracer (which swallows **kwargs) hid the collision and
    gave 202 green tests false confidence."""
    from brain.observability import Tracer

    tracer = Tracer(tmp_path / "t.db", tmp_path / "runs")
    try:
        orch = Orchestrator(session="rt", manager=None, ledger=None, registry=None,
                            blackboard=None, broker=None, planner=None, resolver=None,
                            tracer=tracer)
        ws = tmp_path / "ws"
        (ws / "f").mkdir(parents=True)
        (ws / "f" / "frame.png").write_bytes(b"\x89PNG real-enough bytes")
        res = orch._read_workspace(ws, "f/frame.png")
        assert not res.get("is_error"), res
        assert any(b.get("type") == "image" for b in res["content"])
        # The miss message summarizes what exists (count + first … last) and
        # says outright that it is not absence evidence — a "first 40 of N"
        # cut was observed being read as "only 40 exist" (v0.16).
        for i in range(45):
            (ws / "f" / f"g{i:03d}.txt").write_text("x")
        miss = orch._read_workspace(ws, "nope.png")
        text = miss["content"][0]["text"]
        assert miss.get("is_error") and "46 file(s)" in text
        assert "NOT evidence" in text and "g044.txt" in text
    finally:
        tracer.close()


def test_read_file_internal_error_is_not_a_retry_sink(tmp_path, monkeypatch):
    """An engine fault inside read_file must fail LOUD and self-labeled ('do
    NOT retry … report a blocker'), never an opaque error agents grind on."""
    bb = Blackboard(tmp_path / "b.db", "err")
    orch = Orchestrator(session="err", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=None, resolver=None)
    ws = tmp_path / "ws"
    (ws / "f").mkdir(parents=True)
    (ws / "f" / "frame.png").write_bytes(b"\x89PNG")
    monkeypatch.setattr("brain.capabilities.runtime.image_blocks",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        res = orch._read_workspace(ws, "f/frame.png")
        assert res.get("is_error")
        assert "INTERNAL ERROR" in res["content"][0]["text"]
        assert "do NOT retry" in res["content"][0]["text"]
    finally:
        bb.close()


# --- files named by path are resolved; unreachable ones are flagged to ASK ----- #
def test_designated_paths_resolve_or_flag_for_ask(tmp_path, monkeypatch):
    inbox = tmp_path / "data" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "clip.mp4").write_bytes(b"fake video bytes")
    (tmp_path / "data" / "report.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "INBOX_DIR", inbox)

    # Backticked inbox path resolves and is rewritten to the workspace path.
    text, resolved, unresolved, _g = files_mod.resolve_local_sources(
        "Analyze the video (`data/inbox/clip.mp4`) frame by frame.")
    assert resolved == [str((inbox / "clip.mp4").resolve())]
    assert "input/clip.mp4" in text and "data/inbox/clip.mp4" not in text
    assert unresolved == []
    # Bare basename resolves against the inbox; project-relative path resolves too.
    _t, r2, _u, _g = files_mod.resolve_local_sources("process clip.mp4 and data/report.pdf")
    assert str((inbox / "clip.mp4").resolve()) in r2
    assert str((tmp_path / "data" / "report.pdf").resolve()) in r2
    # A designated file that does NOT resolve is flagged for the ask, not
    # silently dropped and not materialized.
    _t3, r3, u3, _g = files_mod.resolve_local_sources("summarize `data/missing/gone.pdf`")
    assert r3 == [] and u3 == ["data/missing/gone.pdf"]
    # Non-file tokens (counts, abbreviations, URLs) are never treated as files.
    for benign in ("write exactly 3.12 lines", "e.g. do the thing",
                   "see https://x.test/a.pdf now"):
        _tt, rr, uu, _g = files_mod.resolve_local_sources(benign)
        assert rr == [] and uu == []
    # Already-rewritten workspace tokens are left alone.
    _t4, r4, u4, _g = files_mod.resolve_local_sources("see input/clip.mp4")
    assert r4 == [] and u4 == []


def test_missing_file_triggers_interactive_ask(tmp_path, monkeypatch):
    """The credential-style gate, for files: an unreachable designated file
    prompts the user for an accessible path; a good answer is materialized and
    the objective is repointed at the workspace copy."""
    from brain.approval import ApprovalBroker
    from brain.session import BrainSession

    inbox = tmp_path / "data" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "real.mp4").write_bytes(b"actual bytes")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "INBOX_DIR", inbox)

    answers = iter(["data/inbox/real.mp4"])   # the user supplies a good path
    sess = object.__new__(BrainSession)        # bypass __init__ (no Docker/tracer)
    sess.broker = ApprovalBroker(input_fn=lambda _p: next(answers, ""),
                                 output_fn=lambda _t: None)

    obj, resolved, missing = sess._resolve_missing_files(
        "Analyze the video at data/original/clip.mp4 please",
        ["data/original/clip.mp4"], [])
    assert missing == []
    assert resolved == [str((inbox / "real.mp4").resolve())]
    assert "input/real.mp4" in obj                 # objective repointed to the loaded file


def test_missing_file_ask_declined_is_reported_not_retried(tmp_path, monkeypatch):
    from brain.approval import ApprovalBroker
    from brain.session import BrainSession

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    prompts: list[str] = []
    sess = object.__new__(BrainSession)
    # User declines (empty answer) -> the file stays missing, no blind retry.
    sess.broker = ApprovalBroker(input_fn=lambda p: prompts.append(p) or "",
                                 output_fn=lambda _t: None)

    obj, resolved, missing = sess._resolve_missing_files(
        "Summarize gone.pdf", ["gone.pdf"], [])
    assert resolved == [] and missing == ["gone.pdf"]
    assert len(prompts) == 1                        # asked once, accepted the decline
    # An automated (auto-deny) session never blocks on the prompt.
    sess.broker = ApprovalBroker(auto="deny")
    _o, _r, m2 = sess._resolve_missing_files("Summarize gone.pdf", ["gone.pdf"], [])
    assert m2 == ["gone.pdf"]


def test_broker_collect_value_is_visible_and_unstored(tmp_path):
    from brain.approval import ApprovalBroker
    seen = {}
    b = ApprovalBroker(input_fn=lambda p: "data/inbox/x.mp4",
                       output_fn=lambda t: seen.setdefault("out", t))
    val = b.collect_value("a path", "the file could not be accessed",
                          help_text="put it in data/inbox/")
    assert val == "data/inbox/x.mp4"
    assert b.secrets == {}                          # NOT stored as a secret
    # VISIBLE is the other half of the name, and the distinction from
    # collect_secret: the purpose and help text are echoed to the user rather
    # than hidden. The captured output used to be recorded and never asserted.
    assert "the file could not be accessed" in seen["out"]
    assert "data/inbox/" in seen["out"]
    # Automated sessions decline free-form asks.
    assert ApprovalBroker(auto="deny").collect_value("p", "why") is None
