"""What a waiting person is told.

Offline. Every finding covered here came out of a live trial: a real two-turn
conversation against the real model and real Docker, whose stream was measured
line by line. The engine was already recording enough; what it SHOWED was
misleading in three specific ways and silent in a fourth.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from brain.observability.stream import ENGINE_EMISSIONS, format_event
from brain.observability.ticker import Ticker, describe


# --------------------------------------------------------------------------- #
# The engine talking to itself is not progress
# --------------------------------------------------------------------------- #
def test_engine_emissions_are_not_rendered_as_tool_calls():
    """Observed live: the analyst's whole answer appeared as a truncated
    `emit_analysis(...)` line immediately BEFORE the line that says it
    properly, and the plan appeared as a severed JSON blob."""
    for name in ENGINE_EMISSIONS:
        assert format_event({"kind": "agent.tool_call", "name": f"mcp__x__{name}",
                             "status": "ok",
                             "data": {"args_preview": "resolution=satisfied; answer=..."}}) is None
    # A real tool an agent used is still reported.
    line = format_event({"kind": "agent.tool_call", "name": "mcp__caps__web_search",
                         "status": "ok", "data": {"args_preview": "query=python version"}})
    assert line and "web_search" in line and "query=python version" in line


def test_the_emission_list_matches_what_the_engine_actually_defines():
    """A new structured emission must not silently start printing raw. This
    pins the list against the @tool declarations in the engine itself."""
    # Declared either as a decorator or by applying tool(...) to a built body.
    defined = set()
    for path in Path("brain").rglob("*.py"):
        defined |= set(re.findall(r'\btool\(\s*"(emit_[a-z_]+)"', path.read_text()))
    assert defined, "no structured-emission tools found — the scan is broken"
    assert defined == set(ENGINE_EMISSIONS), (
        f"engine defines {sorted(defined)}, stream suppresses {sorted(ENGINE_EMISSIONS)}")


# --------------------------------------------------------------------------- #
# A judging run's exit code is not a verdict
# --------------------------------------------------------------------------- #
def test_a_container_the_engine_ran_to_judge_a_tool_is_not_reported_as_a_failure():
    """Observed live: `ran web_search exit=2` followed by `reuse tool:
    web_search` — which reads as a broken tool being used anyway. Exit 2 was
    the fabrication probe REFUSING, i.e. the passing outcome."""
    for purpose in ("probe", "verify", "revalidate"):
        assert format_event({"kind": "sandbox.run", "name": "web_search", "phase": "end",
                             "status": "ok",
                             "data": {"exit_code": 2, "purpose": purpose}}) is None
    # A tool an AGENT ran still reports its exit code, which is a real signal.
    line = format_event({"kind": "sandbox.run", "name": "web_search", "phase": "end",
                         "status": "ok", "data": {"exit_code": 0}})
    assert line and "exit=0" in line


def test_revalidation_states_its_verdict_in_words():
    start = format_event({"kind": "capability.revalidate", "name": "web_search",
                          "phase": "start", "status": "running",
                          "data": {"stamped": "0.24.4"}})
    assert "still works" in start and "0.24.4" in start

    ok = format_event({"kind": "capability.revalidate", "name": "web_search",
                       "phase": "end", "status": "ok", "data": {"passed": True}})
    assert "still works" in ok and "reusing it" in ok

    bad = format_event({"kind": "capability.revalidate", "name": "web_search",
                        "phase": "end", "status": "ok",
                        "data": {"passed": False, "detail": "fabricated output"}})
    assert "FAILED" in bad and "fabricated output" in bad


# --------------------------------------------------------------------------- #
# What the plan commits the turn to
# --------------------------------------------------------------------------- #
def test_the_plan_announces_whether_tools_must_be_built():
    """Building a tool is minutes and containers; reusing one is seconds. The
    plan line named stages and said nothing about which was coming."""
    reuse = format_event({"kind": "plan.created", "name": "plan", "status": "info", "data": {
        "stages": [{"id": "find", "pattern": "single", "capabilities": ["web_search"]}],
        "known_capabilities": ["web_search", "render_artifact_pages"]}})
    assert "reuses web_search" in reuse and "BUILD" not in reuse

    build = format_event({"kind": "plan.created", "name": "plan", "status": "info", "data": {
        "stages": [{"id": "make", "pattern": "single", "capabilities": ["render_pdf"]}],
        "known_capabilities": ["web_search"]}})
    assert "must BUILD render_pdf" in build and "the slow part" in build


def test_the_plan_says_when_no_tool_is_needed_and_when_a_review_is_coming():
    line = format_event({"kind": "plan.created", "name": "plan", "status": "info", "data": {
        "stages": [{"id": "draft", "pattern": "single", "capabilities": []},
                   {"id": "check", "pattern": "review", "capabilities": []}],
        "known_capabilities": []}})
    assert "no tools" in line and "review round is planned" in line


def test_the_commitment_is_read_off_the_plan_only():
    """It must never become a way of describing the REQUEST."""
    import inspect

    from brain.observability import stream

    sig = inspect.signature(stream._plan_commitment)
    assert list(sig.parameters) == ["stages", "known"]


# --------------------------------------------------------------------------- #
# The ticker: what is happening now, for how long, at what cost
# --------------------------------------------------------------------------- #
def test_the_ticker_reports_elapsed_spend_and_what_is_happening():
    t = Ticker(output_fn=lambda _s: None, interval=15.0)
    t.observe({"kind": "agent.session", "name": "planner", "phase": "start",
               "status": "running", "data": {}})
    t.observe({"kind": "agent.session", "name": "planner", "phase": "end",
               "status": "ok", "data": {"cost_usd": 0.09}})
    t.observe({"kind": "sandbox.run", "name": "web_search", "phase": "start",
               "status": "running", "data": {}})
    line = t.line()
    assert "$0.09 so far" in line
    assert "1 container(s)" in line
    assert "web_search" in line


def test_spend_accumulates_across_sessions_and_is_never_an_estimate():
    """Cost is only known when a session ends, so 'so far' is an honest lower
    bound. A running session must not be guessed at."""
    t = Ticker(output_fn=lambda _s: None)
    t.observe({"kind": "agent.session", "name": "a", "phase": "end",
               "status": "ok", "data": {"cost_usd": 0.25}})
    t.observe({"kind": "agent.session", "name": "b", "phase": "start",
               "status": "running", "data": {}})       # in flight: contributes nothing
    t.observe({"kind": "agent.session", "name": "b", "phase": "end",
               "status": "ok", "data": {"cost_usd": 1.50}})
    assert "$1.75 so far" in t.line()


def test_the_ticker_says_what_it_is_waiting_on_in_human_words():
    cases = {
        ("agent.session", "planner"): "planning the work",
        ("agent.session", "analyst"): "judging what this turn needs",
        ("agent.session", "developer:render_pdf"): "writing the tool render_pdf",
        ("agent.session", "researcher"): "the researcher agent is thinking",
        ("capability.build", "render_pdf"): "writing a new tool",
        ("image.build", "x"): "building a sandbox image",
        ("stage", "gather"): "stage 'gather' is running",
    }
    for (kind, name), expected in cases.items():
        assert describe(kind, name) == expected


def test_an_engine_emission_does_not_read_as_an_agent_using_a_tool():
    """Observed live: while the planner was emitting its plan the ticker said
    'an agent is using a tool' — true of the machinery, false of the work."""
    t = Ticker(output_fn=lambda _s: None)
    t.observe({"kind": "agent.session", "name": "planner", "phase": "start",
               "status": "running", "data": {}})
    t.observe({"kind": "agent.tool_call", "name": "mcp__plan__emit_plan",
               "status": "ok", "data": {}})
    assert "planning the work" in t.line()
    # A real tool call still changes what it says it is doing.
    t.observe({"kind": "agent.tool_call", "name": "mcp__caps__web_search",
               "status": "ok", "data": {}})
    assert "using a tool" in t.line()


def test_quiet_is_distinguished_from_stuck():
    t = Ticker(output_fn=lambda _s: None, interval=15.0, stall_limit=180.0)
    t.last_activity -= 40           # 40 seconds since anything happened
    line = t.line()
    assert "quiet 40s" in line and "aborts at 180s" in line
    # A run that is producing events says nothing about quiet.
    t.observe({"kind": "stage", "name": "gather", "phase": "start", "status": "running"})
    assert "quiet" not in t.line()


def test_the_ticker_prints_on_its_interval():
    printed: list[str] = []

    async def drive():
        t = Ticker(output_fn=printed.append, interval=0.01)
        task = asyncio.create_task(t.loop())
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(drive())
    assert len(printed) >= 2, printed
    assert all(p.startswith("  ⏱") for p in printed)


def test_a_broken_listener_never_takes_the_run_with_it():
    t = Ticker(output_fn=lambda _s: None)
    t.observe({"kind": None, "name": None, "data": "not a dict"})     # malformed
    t.observe({})
    assert t.line()                                                   # still renders


def test_both_entry_points_run_the_ticker():
    """The one-shot path used to print nothing between events either.

    The precedence bug this replaces — `A and B or C` — made A and B dead: both
    modules contain C, so the assertion could not fail. Introduced, with some
    irony, by the commit titled 'replace dead assertions with behaviour'.
    """
    import inspect

    from brain import main as main_mod, repl as repl_mod

    for mod in (main_mod, repl_mod):
        src = inspect.getsource(mod)
        assert "Ticker(" in src, mod.__name__          # constructed
        assert "ticker.observe" in src, mod.__name__   # fed the event stream
        assert "ticker.loop()" in src, mod.__name__    # and actually ticking
