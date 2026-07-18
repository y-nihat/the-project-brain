"""v0.24 — operator-provisioned credentials for unattended runs (PB-T07):
a headless campaign cannot collect a secret interactively when a tool asks,
so the operator pre-provisions it via the environment; the value enters the
broker exactly like a typed one — memory-only, name-only-traced."""
from __future__ import annotations

import asyncio

from brain.approval import ApprovalBroker


class RecTracer:
    def __init__(self): self.events = []
    def event(self, kind, subject, status="info", **data):
        self.events.append((kind, subject, data))


def test_preseed_value_from_env(monkeypatch):
    monkeypatch.setenv("BRAIN_SECRET_KAGGLE_API_TOKEN", "KGAT_verysecret")
    t = RecTracer()
    b = ApprovalBroker(auto="deny", tracer=t)
    loaded = b.preseed_from_env()
    assert loaded == ["KAGGLE_API_TOKEN"]
    # available to a tool that asks — even in auto-deny (pre-provisioned wins)
    assert b.collect_secret("KAGGLE_API_TOKEN", purpose="download") == "KGAT_verysecret"
    # the VALUE is never in the trace; only the name
    blob = repr(t.events)
    assert "KGAT_verysecret" not in blob
    assert "KAGGLE_API_TOKEN" in blob


def test_preseed_from_file(monkeypatch, tmp_path):
    f = tmp_path / "tok"; f.write_text("KGAT_fromfile\n")
    monkeypatch.setenv("BRAIN_SECRET_KAGGLE_API_TOKEN_FILE", str(f))
    b = ApprovalBroker(auto="deny")
    assert b.preseed_from_env() == ["KAGGLE_API_TOKEN"]
    assert b.collect_secret("KAGGLE_API_TOKEN", purpose="x") == "KGAT_fromfile"


def test_preseed_ignores_unrelated_and_empty(monkeypatch):
    monkeypatch.setenv("BRAIN_SECRET_EMPTY", "")
    monkeypatch.setenv("UNRELATED", "x")
    b = ApprovalBroker(auto="deny")
    assert b.preseed_from_env() == []


def test_preseed_does_not_override_existing(monkeypatch):
    monkeypatch.setenv("BRAIN_SECRET_K", "envval")
    b = ApprovalBroker(auto="deny")
    b.secrets["K"] = "already"
    b.preseed_from_env()
    assert b.secrets["K"] == "already"


# --- v0.24.1: deliverable filenames not misread as inputs; _run_ctx crash ----------- #
def test_deliverable_filenames_not_treated_as_missing_inputs():
    """PB-T07 crashed: 'produce submission.csv / method_report.md' were resolved
    as missing INPUT files, the engine told the agent to fatal-block on files it
    must CREATE, and the early-block path crashed on _run_ctx."""
    from brain.files import resolve_local_sources
    obj = ("Produce a submission.\n## Deliverables\n1. `submission.csv` - format.\n"
           "2. `method_report.md` - writeup.\n3. pipeline regenerating `submission.csv`.")
    _, resolved, unresolved = resolve_local_sources(obj)
    assert unresolved == [], unresolved   # all are deliverables, not inputs


def test_real_missing_input_still_flagged():
    from brain.files import resolve_local_sources
    _, _, u = resolve_local_sources("Analyze the attached data.csv and summarize.")
    assert "data.csv" in u
    # a verb far from the token (not its immediate object) => still an input
    _, _, u2 = resolve_local_sources("Produce a chart from the source.pdf provided.")
    assert "source.pdf" in u2


def test_run_ctx_initialized_on_construction():
    """The early-failure/help paths guard with `if self._run_ctx`; the attribute
    must exist even if a run fails before run() sets it."""
    from brain.orchestrator import Orchestrator
    o = object.__new__(Orchestrator)
    # __init__ path sets it; simulate the reset attribute presence
    assert not hasattr(o, "_run_ctx") or o._run_ctx is None or o._run_ctx
    # the real guarantee: a fresh Orchestrator has the attribute
    from brain.planner import StubPlanner
    orch = Orchestrator(session="x", manager=None, ledger=None, registry=None,
                        blackboard=None, broker=None, planner=StubPlanner([]),
                        resolver=None)
    assert orch._run_ctx is None


# --- v0.24.2: credential-usage contract + filename-global deliverable skip ----------- #
def test_deliverable_skip_is_filename_global():
    """A filename that is a deliverable ANYWHERE (under Deliverables) must not
    be read as an input at its OTHER mentions ('pipeline regenerates X')."""
    from brain.files import resolve_local_sources
    obj = ("Produce a submission.\n## Deliverables\n1. `submission.csv` - format.\n"
           "2. `report.md` - writeup.\n3. the pipeline that regenerates "
           "`submission.csv` end-to-end.")
    _, _, unresolved = resolve_local_sources(obj)
    assert unresolved == [], unresolved


def test_credential_contract_presumes_provided_valid():
    from brain.acquisition import _DEVELOPER_SYSTEM as s
    assert "CREDENTIAL IS PRESUMED VALID" in s
    assert "documented authentication method" in s
    assert "username:key" in s   # names the exact over-assumption to avoid


def test_run_ctx_set_before_pre_stage_submethods(tmp_path, monkeypatch):
    """A cross-run-seeded cycle crashed 'cannot unpack NoneType' because a
    pre-stage sub-method (research/knowledge/proxy) unpacked _run_ctx before
    run() set it. It must be valid as soon as the workspace exists."""
    from brain import config
    from brain.memory.blackboard import Blackboard
    from brain.observability import NullTracer
    from brain.orchestrator import Orchestrator
    from brain.planner import StubPlanner
    from brain.models import StageSpec

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    captured = {}

    async def runner(_t, name, prompt, options):
        # a research/proxy sub-method would unpack _run_ctx; assert it's set
        captured["ctx_at_run"] = orch._run_ctx
        return "```deliverable\nok\n```"

    planner = StubPlanner([StageSpec(id="s", pattern="single", role="W",
                                     goal="do", deliverable=True)])
    bb = Blackboard(tmp_path / "rc.db", "rc1")
    orch = Orchestrator(session="rc1", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner, tracer=NullTracer())
    try:
        asyncio.run(orch.run("do the thing"))
    finally:
        bb.close()
    assert captured["ctx_at_run"] is not None
    assert len(captured["ctx_at_run"]) == 3   # (objective, history, workspace)
