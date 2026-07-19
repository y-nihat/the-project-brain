"""Research delegation channel (Component 1): an agent that recognizes it lacks
KNOWLEDGE asks the orchestrator to research it — mediated, bounded, cached — and
the findings come back labeled as METHOD KNOWLEDGE, never source evidence. The
channel reaches stage agents AND developer/repair agents (which previously had
no orchestrator channel at all). Stubbed — live behavior is validated in runs."""
from __future__ import annotations

import asyncio

import pytest

from brain import acquisition, config
from brain.memory.blackboard import Blackboard
from brain.models import AgentSpec
from brain.observability import NullTracer
from brain.orchestrator import Orchestrator
from brain.team import TEAM_TOOLS, TeamChannel, build_team_server


# --- the channel primitive ------------------------------------------------------ #
def test_team_channel_research_delegates_and_guards(tmp_path):
    calls: list[tuple] = []

    async def research_fn(topic, why, specifics):
        calls.append((topic, why, specifics))
        return "findings: do X"

    ch = TeamChannel(agent_id="w1", completed_stages=[], ask_fn=None,
                     research_fn=research_fn)
    ok, text = asyncio.run(ch.research("how to parse an odd video container",
                                       "my tool errors", "moov atom not found"))
    assert ok and text == "findings: do X"
    assert calls == [("how to parse an odd video container", "my tool errors",
                      "moov atom not found")]
    # Too-short topic is refused without calling out.
    ok2, _ = asyncio.run(ch.research("x"))
    assert not ok2 and len(calls) == 1
    # No research_fn -> unavailable, not a crash.
    ch2 = TeamChannel(agent_id="w", completed_stages=[], ask_fn=None)
    ok3, msg = asyncio.run(ch2.research("a real topic"))
    assert not ok3 and "not available" in msg


def test_request_research_is_a_team_tool():
    assert "mcp__team__request_research" in TEAM_TOOLS
    # The server builds cleanly with the new tool included (SDK server is an
    # opaque dict; the tool body just forwards to channel.research, covered above).
    ch = TeamChannel(agent_id="w", completed_stages=[], ask_fn=None,
                     research_fn=lambda *a: None)
    server = build_team_server(ch)
    assert server.get("name") == "team"


# --- the orchestrator delegate: budget, cache, honesty label -------------------- #
def _orch(tmp_path, runner):
    bb = Blackboard(tmp_path / "b.db", "res")
    orch = Orchestrator(session="res", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=None, resolver=None,
                        session_runner=runner, tracer=NullTracer())
    orch._run_ctx = ("the objective", None, tmp_path / "ws")
    return orch, bb


def test_research_runs_an_agent_labels_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    ran: list[str] = []

    async def runner(_t, name, prompt, options):
        ran.append(name)
        assert "TOPIC:" in prompt                      # the research brief prompt
        return "Use ffmpeg -movflags +faststart; the moov atom is at the end."

    orch, bb = _orch(tmp_path, runner)
    # A research agent gets web_search; stub acquisition so no Docker is needed.
    monkeypatch.setattr(orch, "_acquire_caps", lambda spec: _aret({}))
    try:
        out = asyncio.run(orch._research("ffmpeg moov atom not found",
                                         "audio extraction tool exits 1", "moov atom not found"))
    finally:
        bb.close()
    assert "METHOD KNOWLEDGE" in out and "NOT source evidence" in out
    assert "faststart" in out
    assert orch._research_used == 1 and ran == ["research.1"]
    # Same topic -> cache hit, no second agent.
    out2 = asyncio.run(orch._research("FFMPEG   moov   atom   not found"))  # normalized key
    assert out2 == out and orch._research_used == 1 and ran == ["research.1"]


def test_research_budget_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "RESEARCH_MAX_REQUESTS", 1)
    ran: list[str] = []

    async def runner(_t, name, prompt, options):
        ran.append(name)
        return "findings"

    orch, bb = _orch(tmp_path, runner)
    monkeypatch.setattr(orch, "_acquire_caps", lambda spec: _aret({}))
    try:
        asyncio.run(orch._research("topic one"))
        exhausted = asyncio.run(orch._research("topic two"))   # over budget
    finally:
        bb.close()
    assert orch._research_used == 1 and ran == ["research.1"]
    assert "budget exhausted" in exhausted


def _aret(v):
    async def _c():
        return v
    return _c()


# --- developer agents finally get the channel ----------------------------------- #
def test_developer_agent_gets_request_research_only_when_provided(tmp_path, monkeypatch):
    """The exact gap this closes: developer/repair agents had NO orchestrator
    channel. With a research_fn they get request_research; without one they
    don't (unchanged)."""
    captured: dict = {}

    async def fake_trace_query(tracer, name, task, options, **kw):
        captured["allowed"] = list(getattr(options, "allowed_tools", []) or [])
        return ""

    monkeypatch.setattr(acquisition, "trace_query", fake_trace_query)

    async def research_fn(topic, why, specifics):
        return "brief"

    dev = acquisition.make_developer_agent(research_fn=research_fn)
    asyncio.run(dev("some_tool", "a tool"))
    assert "mcp__dev__request_research" in captured["allowed"]

    plain = acquisition.make_developer_agent()          # no research_fn
    asyncio.run(plain("some_tool", "a tool"))
    assert "mcp__dev__request_research" not in captured["allowed"]


def test_developer_system_prompt_teaches_research(tmp_path, monkeypatch):
    """When a research_fn is present, the developer is told to research an
    unfamiliar library/error before retrying blindly."""
    captured: dict = {}

    async def fake_trace_query(tracer, name, task, options, **kw):
        captured["prompt"] = getattr(options, "system_prompt", "") or ""
        return ""

    monkeypatch.setattr(acquisition, "trace_query", fake_trace_query)

    async def research_fn(*a):
        return "brief"

    asyncio.run(acquisition.make_developer_agent(research_fn=research_fn)("k", "d"))
    assert "request_research" in captured["prompt"]
    asyncio.run(acquisition.make_developer_agent()("k", "d"))   # none -> no hint
    assert "request_research" not in captured["prompt"]


def test_acquire_threads_research_fn_to_the_developer(tmp_path, monkeypatch):
    """acquire() passes research_fn down to whoever builds the developer agent."""
    from brain.registry import Registry

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    seen: dict = {}

    async def stub_developer(need_key, description, previous=None, error=None):
        return None  # forces AcquisitionError; we only assert what was threaded

    def fake_make(model=None, tracer=None, research_fn=None):
        seen["research_fn"] = research_fn
        return stub_developer

    monkeypatch.setattr(acquisition, "make_developer_agent", fake_make)

    called: list = []

    async def research_fn(*a):
        called.append(a)
        return "x"

    with pytest.raises(acquisition.AcquisitionError):
        asyncio.run(acquisition.acquire("novel_cap", manager=None, registry=reg, broker=None,
                                        tracer=NullTracer(), research_fn=research_fn))
    reg.close()
    # The authoring loop wraps research_fn (research delivery opens a new
    # information epoch), so assert DELEGATION, not identity: the threaded
    # callable must reach the original and return its value.
    threaded = seen["research_fn"]
    assert threaded is not None
    assert asyncio.run(threaded("topic")) == "x"
    assert called and called[-1] == ("topic",)
