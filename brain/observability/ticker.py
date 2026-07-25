"""The live ticker: what is happening, for how long, and what it has cost.

A run is a long silence punctuated by events. The events say what just
happened; nothing said what is happening NOW, how long it has been happening,
or what the turn has spent — and spend is the number a person most wants while
a run is still cancellable, not after. Measured on a real run: the longest
silent stretch was 15s in a 92-second turn, against a recorded corpus whose
median turn is 9 minutes and whose p90 is 34.

So: one line every ``INTERVAL`` seconds, unconditionally. Not a stall warning —
a stall warning only appears when something is wrong, which means a healthy
long run stays silent, which is the exact case this exists for.

It reads the same trace records the stream renders, so it needs no plumbing
into the engine and cannot disagree with what the engine recorded.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

INTERVAL = 15.0

# What the engine is doing, said the way a person would say it. Keyed by the
# trace record's kind (and, where the kind is too coarse, its name), because
# these are the engine's OWN activity vocabulary — not anything about the task.
_DOING = {
    "analysis.verdict": "judging what this turn needs",
    "plan": "planning the work",
    "agent.session": "an agent is thinking",
    "sandbox.run": "running a tool in a sandbox",
    "image.build": "building a sandbox image",
    "capability.build": "writing a new tool",
    "capability.verify": "checking the new tool actually works",
    "capability.revalidate": "re-checking a remembered tool",
    "stage": "a stage is running",
    "review.verdict": "reviewing the draft",
    "supervisor.consult": "the supervisor is judging the result",
    "agent.tool_call": "an agent is using a tool",
    "map.fanout": "workers are processing items in parallel",
}


def describe(kind: str, name: str = "") -> str:
    """One human phrase for what the engine is currently occupied with."""
    if kind == "agent.session":
        if name.startswith("developer:"):
            return f"writing the tool {name.split(':', 1)[1]}"
        if name == "planner":
            return _DOING["plan"]
        if name == "analyst":
            return _DOING["analysis.verdict"]
        if name.startswith("supervisor"):
            return "the supervisor is judging"
        return f"the {name} agent is thinking" if name else "an agent is thinking"
    if kind == "stage":
        return f"stage '{name}' is running" if name else _DOING["stage"]
    if kind == "sandbox.run":
        return f"running {name} in a sandbox" if name else _DOING["sandbox.run"]
    return _DOING.get(kind, f"{kind}/{name}" if name else kind)


class Ticker:
    """Accumulates run state from trace records and prints a periodic line.

    Attach :meth:`observe` as a tracer listener and run :meth:`loop` as a task
    for the duration of a turn.
    """

    def __init__(self, *, output_fn: Callable[[str], None] = print,
                 interval: float = INTERVAL, stall_limit: Optional[float] = None):
        self.output_fn = output_fn
        self.interval = interval
        self.stall_limit = stall_limit
        self.reset()

    def reset(self) -> None:
        self.started = time.monotonic()
        self.last_activity = self.started
        self.doing = "starting"
        self.cost_usd = 0.0
        self.containers = 0
        self.tools_built = 0

    # -- state ------------------------------------------------------------ #
    def observe(self, record: dict) -> None:
        """One trace record in; internal state updated. Never raises — a
        listener that breaks would take the run with it."""
        try:
            kind = str(record.get("kind") or "")
            name = str(record.get("name") or "")
            data = record.get("data") or {}
            self.last_activity = time.monotonic()
            # The engine's own structured emissions are not an agent using a
            # tool. Observed live: while the planner was emitting its plan the
            # ticker reported "an agent is using a tool", which is true of the
            # machinery and false of the work.
            from .stream import ENGINE_EMISSIONS

            engine_emission = (kind == "agent.tool_call"
                               and name.split("__")[-1] in ENGINE_EMISSIONS)
            if (kind in _DOING or kind == "agent.session") and not engine_emission:
                self.doing = describe(kind, name)
            # Spend is only known when a session ENDS, so "so far" is exactly
            # what this reports — an honest lower bound, never an estimate.
            if kind == "agent.session" and record.get("phase") == "end":
                cost = data.get("cost_usd")
                if isinstance(cost, (int, float)):
                    self.cost_usd += float(cost)
            if kind == "sandbox.run" and record.get("phase") != "end":
                self.containers += 1
            if kind == "capability.promote" and record.get("phase") != "start":
                self.tools_built += 1
        except Exception:  # noqa: BLE001
            pass

    # -- rendering -------------------------------------------------------- #
    def line(self) -> str:
        now = time.monotonic()
        elapsed = now - self.started
        quiet = now - self.last_activity
        mins, secs = divmod(int(elapsed), 60)
        parts = [f"{mins}m{secs:02d}s" if mins else f"{secs}s",
                 f"${self.cost_usd:.2f} so far",
                 self.doing]
        if self.containers:
            parts.insert(2, f"{self.containers} container(s)")
        if self.tools_built:
            parts.insert(2, f"{self.tools_built} tool(s) built")
        line = "  ⏱ " + "  ·  ".join(parts)
        # Quiet is not the same as stuck, and the difference is a fact the
        # engine knows: the watchdog's own budget.
        if quiet >= self.interval:
            line += f" (quiet {int(quiet)}s"
            if self.stall_limit:
                line += f"; aborts at {int(self.stall_limit)}s of true silence"
            line += ")"
        return line

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                self.output_fn(self.line())
            except Exception:  # noqa: BLE001
                pass
