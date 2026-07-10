"""Runtime health of trusted tools.

A capability can pass verification at birth yet degrade in production — e.g.
`web_search` starts returning empty results because a backend rate-limits it.
This module classifies each tool result and tracks per-capability health within a
single agent attempt so the orchestrator can reflect: repair a degraded tool
instead of letting an agent grind it forever.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from .. import config

# Result classes.
OK = "ok"
EMPTY = "empty"       # ran fine but returned no data (the silent failure)
ERROR = "error"       # the tool reported an error


def classify_result(stdout: str, *, is_error: bool = False) -> tuple[str, int]:
    """Classify a tool's output as ok|empty|error, with a rough item count.

    Generic 'empty' heuristic: parseable JSON object with at least one top-level
    list field where every such field is empty (catches {"query":…,"results":[]}),
    or a body that is just an error. Non-JSON / scalar output is treated as ok
    (we only judge structured, data-returning tools).
    """
    if is_error:
        return ERROR, 0
    text = (stdout or "").strip()
    if not text:
        return EMPTY, 0
    try:
        data = json.loads(text.splitlines()[-1])
    except Exception:  # noqa: BLE001
        return OK, 0  # unstructured output — not ours to judge
    if isinstance(data, dict):
        if set(data.keys()) == {"error"} or (data.get("error") and len(data) <= 2):
            return ERROR, 0
        lists = [v for v in data.values() if isinstance(v, list)]
        if lists:
            total = sum(len(v) for v in lists)
            return (EMPTY, 0) if total == 0 else (OK, total)
        return OK, 1
    if isinstance(data, list):
        return (EMPTY, 0) if len(data) == 0 else (OK, len(data))
    return OK, 1


@dataclass
class _CapStat:
    calls: int = 0
    empties: int = 0
    errors: int = 0
    consecutive_empty: int = 0
    last_reason: str = ""


@dataclass
class HealthMonitor:
    """Per-agent-attempt view of each capability's live health + call budget."""

    call_budget: int = field(default_factory=lambda: config.CAPABILITY_CALL_BUDGET)
    _stats: dict[str, _CapStat] = field(default_factory=dict)
    # Causal attribution evidence: cap -> True while EVERY observed write matched
    # the content it was given byte-for-byte. A verbatim conduit cannot have
    # introduced a content defect, so it must never be blamed for one.
    verbatim: dict[str, bool] = field(default_factory=dict)

    def _stat(self, cap: str) -> _CapStat:
        return self._stats.setdefault(cap, _CapStat())

    def over_budget(self, cap: str) -> bool:
        return self._stat(cap).calls >= self.call_budget

    def record(self, cap: str, result_class: str) -> None:
        s = self._stat(cap)
        s.calls += 1
        if result_class == EMPTY:
            s.empties += 1
            s.consecutive_empty += 1
        elif result_class == ERROR:
            s.errors += 1
            s.consecutive_empty = 0
        else:
            s.consecutive_empty = 0

    def note_verbatim(self, cap: str, is_verbatim: bool) -> None:
        """Record whether a write-producing call persisted its given content
        exactly (one non-verbatim call clears the exoneration for good)."""
        self.verbatim[cap] = self.verbatim.get(cap, True) and is_verbatim

    def unhealthy(self, cap: str) -> Optional[str]:
        """A human-readable reason the capability looks broken, or None."""
        s = self._stats.get(cap)
        if not s or s.calls == 0:
            return None
        bad = s.empties + s.errors
        if s.consecutive_empty >= config.CAPABILITY_UNHEALTHY_CONSECUTIVE_EMPTY:
            return (f"returned empty {s.consecutive_empty} times in a row "
                    f"(likely rate-limited or a broken backend)")
        if s.calls >= config.CAPABILITY_UNHEALTHY_MIN_CALLS and \
                bad / s.calls >= config.CAPABILITY_UNHEALTHY_EMPTY_RATIO:
            return (f"{bad} of {s.calls} calls returned empty/error "
                    f"({bad / s.calls:.0%}) — the tool is not delivering results")
        return None

    def summary(self, cap: str) -> str:
        s = self._stats.get(cap)
        if not s:
            return "no calls"
        return f"{s.calls} calls, {s.empties} empty, {s.errors} error"
