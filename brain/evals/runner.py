"""Run scenarios live and/or score runs post-hoc; write scorecards.

A scorecard is written per suite invocation under ``data/evals/<stamp>/`` as
Markdown (for humans) and JSON (for diffing between engine versions).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import config
from .checks import CHECKS
from .facts import RunFacts, load_facts
from .scenarios import SCENARIOS, Scenario, get


@dataclass
class ScoreCard:
    scenario_id: str
    run_id: str
    passed: int = 0
    failed: int = 0
    results: list[dict] = field(default_factory=list)   # {check, params, passed, detail}
    facts_summary: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.failed == 0


def score_run(run_id: str, scenario: Scenario | str,
              db_path=None, runs_dir=None) -> ScoreCard:
    """Apply a scenario's checks to an existing run (no LLM, no cost)."""
    if isinstance(scenario, str):
        scenario = get(scenario)
    facts = load_facts(db_path or config.DB_PATH, runs_dir or config.RUNS_DIR, run_id)
    card = ScoreCard(scenario_id=scenario.id, run_id=run_id)
    for name, params in scenario.checks:
        ok, detail = CHECKS[name](facts, **params)
        card.results.append({"check": name, "params": params, "passed": ok, "detail": detail})
        card.passed += ok
        card.failed += not ok
    card.facts_summary = {
        "status": facts.status, "cost_usd": round(facts.cost_usd, 2),
        "duration_s": round(facts.duration_ms / 1000, 1), "sessions": facts.sessions,
        "sandbox_runs": facts.sandbox_runs, "stages": [s.get("id") for s in facts.stages],
        "patterns": sorted(facts.patterns_used), "fanout_workers": facts.fanout_workers,
        "tool_results": dict(facts.tool_results), "urls_cited": len(facts.cited_urls),
        "repairs": facts.repairs,
    }
    return card


async def run_suite(scenario_ids: Optional[list[str]] = None, *, session=None) -> tuple[list[ScoreCard], Path]:
    """Run scenarios LIVE through a BrainSession, score each, write scorecards.

    Costs real tokens and minutes; run it after major engine changes, not per
    commit. A shared ``session`` may be injected (tests use a stub)."""
    from ..session import BrainSession

    chosen = [get(sid) for sid in scenario_ids] if scenario_ids else list(SCENARIOS)
    own_session = session is None
    session = session or BrainSession(auto_export=False)
    cards: list[ScoreCard] = []
    try:
        for sc in chosen:
            summary = await session.run_objective(sc.objective)
            cards.append(score_run(summary.run_id, sc))
    finally:
        if own_session:
            session.close()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(config.DATA_DIR) / "evals" / f"eval-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scorecard.json").write_text(json.dumps({
        "engine_version": config.ENGINE_VERSION,
        "stamp": stamp,
        "cards": [card.__dict__ for card in cards],
    }, indent=2, default=str))
    (out_dir / "scorecard.md").write_text(render_markdown(cards))
    return cards, out_dir


def render_markdown(cards: list[ScoreCard]) -> str:
    lines = [f"# Eval scorecard — engine {config.ENGINE_VERSION}", ""]
    total_p = sum(c.passed for c in cards)
    total_f = sum(c.failed for c in cards)
    lines.append(f"**{total_p} passed / {total_f} failed** across {len(cards)} scenario(s)\n")
    for c in cards:
        icon = "✅" if c.ok else "❌"
        lines.append(f"## {icon} {c.scenario_id} — run `{c.run_id}`")
        fs = c.facts_summary
        if fs:
            lines.append(f"*{fs.get('status')} · ${fs.get('cost_usd')} · {fs.get('duration_s')}s · "
                         f"{fs.get('sessions')} sessions · patterns {fs.get('patterns')} · "
                         f"{fs.get('urls_cited')} URLs cited*")
        lines.append("")
        lines.append("| check | result | detail |")
        lines.append("|---|---|---|")
        for r in c.results:
            mark = "✓" if r["passed"] else "✗"
            lines.append(f"| {r['check']} | {mark} | {r['detail']} |")
        lines.append("")
    return "\n".join(lines)
