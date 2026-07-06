"""Deterministic checks over a run's facts.

Each check is `(facts, **params) -> (passed, detail)`. Scenarios reference them
by name; unknown names fail loudly at definition-validation time, not mid-eval.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Callable

from .facts import RunFacts

CheckFn = Callable[..., tuple[bool, str]]
CHECKS: dict[str, CheckFn] = {}


def _check(fn: CheckFn) -> CheckFn:
    CHECKS[fn.__name__] = fn
    return fn


@_check
def status_is(f: RunFacts, expected: str = "succeeded"):
    return f.status == expected, f"status={f.status!r} (want {expected!r})"


@_check
def max_cost_usd(f: RunFacts, limit: float):
    return f.cost_usd <= limit, f"cost=${f.cost_usd:.2f} (limit ${limit:.2f})"


@_check
def max_sessions(f: RunFacts, limit: int):
    return f.sessions <= limit, f"sessions={f.sessions} (limit {limit})"


@_check
def max_stages(f: RunFacts, limit: int):
    return len(f.stages) <= limit, f"stages={len(f.stages)} (limit {limit})"


@_check
def used_pattern(f: RunFacts, pattern: str):
    return pattern in f.patterns_used, f"patterns={sorted(f.patterns_used)} (want {pattern!r})"


@_check
def min_workers(f: RunFacts, n: int):
    return f.fanout_workers >= n, f"fan-out width={f.fanout_workers} (want >= {n})"


@_check
def review_ran(f: RunFacts):
    return bool(f.review_verdicts), f"verdicts={f.review_verdicts}"


@_check
def no_tools_used(f: RunFacts):
    return f.sandbox_runs == 0, f"sandbox runs={f.sandbox_runs} (want 0)"


@_check
def tool_ok_calls(f: RunFacts, min_count: int = 1):
    ok = f.tool_results.get("ok", 0)
    return ok >= min_count, f"ok tool results={ok} (want >= {min_count})"


@_check
def tool_error_ratio_below(f: RunFacts, limit: float = 0.5):
    total = sum(f.tool_results.values())
    bad = f.tool_results.get("error", 0) + f.tool_results.get("empty", 0)
    ratio = (bad / total) if total else 0.0
    return ratio <= limit, f"empty/error ratio={ratio:.2f} of {total} calls (limit {limit})"


@_check
def cites_url(f: RunFacts, min_count: int = 1):
    n = len(f.cited_urls)
    return n >= min_count, f"distinct URLs cited={n} (want >= {min_count})"


@_check
def answer_matches(f: RunFacts, regex: str):
    hit = re.search(regex, f.answer, re.IGNORECASE | re.DOTALL) is not None
    return hit, f"answer {'matches' if hit else 'does not match'} /{regex}/"


@_check
def answer_max_chars(f: RunFacts, limit: int):
    return len(f.answer) <= limit, f"answer length={len(f.answer)} (limit {limit})"


@_check
def answer_min_chars(f: RunFacts, limit: int):
    return len(f.answer) >= limit, f"answer length={len(f.answer)} (want >= {limit})"


@_check
def produced_file(f: RunFacts, glob: str):
    hits = [p for p in f.workspace_files if fnmatch.fnmatch(p, glob)]
    return bool(hits), f"workspace files matching {glob!r}: {hits[:3] or 'none'}"


@_check
def capability_created_or_reused(f: RunFacts, name_contains: str = ""):
    pool = f.capabilities_created + f.capabilities_reused
    hits = [c for c in pool if name_contains in c]
    return bool(hits), f"capabilities touched: {pool} (want one containing {name_contains!r})"
