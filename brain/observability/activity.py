"""Delegated-work beacon: lets the stall watchdog distinguish a SILENT agent
session from one that is WAITING on engine-side work done on its behalf.

Observed failure: a developer session requested research; the research ran for
minutes (legitimately), no SDK message reached the developer's stream, and the
stall watchdog killed the developer at 180s — discarding the in-flight
research with the cascade. Waiting on delegated work is not silence.

The beacon is process-global on purpose: sessions, sandbox runs and research
all share one event loop, and the cost of a false reprieve (a genuinely hung
session surviving until its TOTAL budget while unrelated work runs) is far
smaller than the observed cost of a false kill. The total session budget is
never suspended.
"""
from __future__ import annotations

import time

_depth = 0
_last_done = 0.0


class working:
    """Context manager marking a window of engine-side delegated work
    (research, a sandbox run, an image build, an upstream-question session)."""

    def __enter__(self):
        global _depth
        _depth += 1
        return self

    def __exit__(self, *exc):
        global _depth, _last_done
        _depth = max(0, _depth - 1)
        _last_done = time.monotonic()
        return False


def in_flight() -> bool:
    """True while any delegated work is running."""
    return _depth > 0


def finished_within(seconds: float) -> bool:
    """True when delegated work completed less than ``seconds`` ago — the
    waiting session deserves one fresh stall window to receive the result."""
    return (time.monotonic() - _last_done) < seconds
