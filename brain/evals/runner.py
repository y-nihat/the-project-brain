"""Run scenarios live and/or score runs post-hoc; write scorecards.

A scorecard is written per suite invocation under ``data/evals/<stamp>/`` as
Markdown (for humans) and JSON (for diffing between engine versions).
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
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


async def run_suite(scenario_ids: Optional[list[str]] = None, *, session=None,
                    echo: bool = False) -> tuple[list[ScoreCard], Path]:
    """Run scenarios LIVE through a BrainSession, score each, write scorecards.

    Costs real tokens and minutes; run it after major engine changes, not per
    commit. A shared ``session`` may be injected (tests use a stub);
    ``echo`` streams the same live progress lines the REPL shows."""
    from ..session import BrainSession

    chosen = [get(sid) for sid in scenario_ids] if scenario_ids else list(SCENARIOS)
    own_session = session is None
    session = session or BrainSession(auto_export=False)
    if echo and hasattr(session, "add_listener"):
        from ..repl import format_event

        def _echo(rec: dict) -> None:
            line = format_event(rec)
            if line:
                print(line, flush=True)

        session.add_listener(_echo)
    cards: list[ScoreCard] = []
    try:
        for sc in chosen:
            if echo:
                print(f"\n⟳ scenario {sc.id}: {sc.objective}", flush=True)
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


def score_corpus(scenario_id: str = "engine-health", db_path=None, runs_dir=None
                 ) -> list[ScoreCard]:
    """Score every recorded run offline — no LLM, no containers, no cost.

    45 releases shipped with no cross-version behavioural baseline, and the
    harness built to provide one had not run since engine 0.1.0. The reason it
    stayed unused is that running it LIVE costs real money and minutes; but
    `score_run` never needed either. The runs already happened, their events
    are on disk, and an objective-neutral scenario can be applied to all of
    them retroactively.
    """
    import sqlite3

    db = db_path or config.DB_PATH
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        run_ids = [r["run_id"] for r in conn.execute(
            "SELECT run_id FROM runs ORDER BY started_at")]
    finally:
        conn.close()
    cards = []
    for rid in run_ids:
        try:
            cards.append(score_run(rid, scenario_id, db_path=db, runs_dir=runs_dir))
        except Exception:  # noqa: BLE001 — one unreadable run must not stop the sweep
            continue
    return cards


def render_corpus(cards: list[ScoreCard], db_path=None) -> str:
    """Per-engine-version roll-up of an offline corpus sweep."""
    import sqlite3
    from collections import defaultdict

    db = db_path or config.DB_PATH
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        meta = {r["run_id"]: r for r in conn.execute(
            "SELECT run_id, engine_version, status FROM runs")}
    finally:
        conn.close()

    by_version: dict = defaultdict(lambda: {"runs": 0, "ok": 0, "failed_checks": Counter()})
    for card in cards:
        row = meta.get(card.run_id)
        if not row or not row["engine_version"]:
            continue
        ver = str(row["engine_version"]).split("@")[0].split("+")[0]
        slot = by_version[ver]
        slot["runs"] += 1
        slot["ok"] += card.ok
        for res in card.results:
            if not res["passed"]:
                slot["failed_checks"][res["check"]] += 1

    lines = [f"# Offline corpus sweep — {len(cards)} run(s), $0", "",
             "| engine | runs | clean | tripped checks |",
             "| --- | ---: | ---: | --- |"]
    for ver in sorted(by_version):
        s = by_version[ver]
        tripped = ", ".join(f"{k}x{v}" for k, v in s["failed_checks"].most_common()) or "-"
        lines.append(f"| {ver} | {s['runs']} | {s['ok']} | {tripped} |")
    lines += ["", "_Objective-neutral checks only: this is a regression tripwire "
                  "across versions, not a quality score. Task difficulty is not "
                  "constant across versions._"]
    return "\n".join(lines)
