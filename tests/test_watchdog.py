"""Agent-session watchdog + startup janitor (pure; no Docker/LLM)."""
from __future__ import annotations

import asyncio

import pytest

from brain.ledger import Ledger
from brain.observability import Tracer
from brain.observability.tracer import AgentStallError, _iter_with_watchdog
from brain.sandbox import policy


def test_watchdog_raises_on_stall():
    async def stalling():
        yield "m1"
        await asyncio.sleep(10)  # never delivers m2 within the stall window
        yield "m2"

    async def run():
        seen = []
        with pytest.raises(AgentStallError) as e:
            async for m in _iter_with_watchdog(stalling(), 0.2, 5, "agent 'x'"):
                seen.append(m)
        assert seen == ["m1"]
        assert "no activity" in str(e.value)

    asyncio.run(run())


def test_watchdog_passes_a_normal_stream():
    async def ok():
        for i in range(3):
            yield i

    async def run():
        assert [m async for m in _iter_with_watchdog(ok(), 1, 5, "x")] == [0, 1, 2]

    asyncio.run(run())


def test_watchdog_enforces_total_budget():
    async def steady():
        while True:
            await asyncio.sleep(0.02)
            yield "tick"

    async def run():
        with pytest.raises(AgentStallError) as e:
            async for _ in _iter_with_watchdog(steady(), 1, 0.15, "agent 'y'"):
                pass
        assert "budget" in str(e.value)

    asyncio.run(run())


def test_janitor_heals_stale_records(tmp_path):
    # A killed process leaves a run 'running' and a ledger row 'running'.
    tracer = Tracer(tmp_path / "b.db", tmp_path / "runs")
    tracer.start_run("left dangling")            # never finished
    assert tracer.abort_stale_runs() == 1
    assert tracer.abort_stale_runs() == 0        # idempotent
    tracer.close()

    led = Ledger(tmp_path / "l.db")
    led.record_start(id="c9", session="s", capability=None, image="i",
                     command=["x"], policy=policy.isolated())
    assert led.find_leaks() != []
    assert led.mark_stale_reaped() == 1
    assert led.find_leaks() == []
    led.close()
