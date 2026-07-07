"""Extract a completed run's observable facts from the trace DB + data layer.

Everything a check may score lives here, computed once — checks stay pure
functions over :class:`RunFacts`.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


@dataclass
class RunFacts:
    run_id: str
    objective: str = ""
    status: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0
    n_containers: int = 0
    stages: list[dict] = field(default_factory=list)      # from plan.created
    sessions: int = 0                                      # agent LLM sessions
    sandbox_runs: int = 0                                  # tool executions
    tool_results: Counter = field(default_factory=Counter)  # ok/empty/error
    fanout_workers: int = 0                                # max map fan-out width
    review_verdicts: list[str] = field(default_factory=list)
    capabilities_created: list[str] = field(default_factory=list)
    capabilities_reused: list[str] = field(default_factory=list)
    repairs: int = 0
    team_questions: int = 0
    team_blockers: int = 0
    salvaged_sessions: int = 0
    events_by_kind: Counter = field(default_factory=Counter)
    answer: str = ""
    workspace_files: list[str] = field(default_factory=list)

    @property
    def cited_urls(self) -> list[str]:
        return list(dict.fromkeys(_URL_RE.findall(self.answer)))

    @property
    def patterns_used(self) -> set[str]:
        return {s.get("pattern", "single") for s in self.stages}


def load_facts(db_path: Path | str, runs_dir: Path | str, run_id: str) -> RunFacts:
    facts = RunFacts(run_id=run_id)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"run '{run_id}' not found in {db_path}")
        facts.objective = row["objective"] or ""
        facts.status = row["status"] or ""
        facts.cost_usd = row["cost_usd"] or 0.0
        facts.duration_ms = row["duration_ms"] or 0
        facts.n_containers = row["n_containers"] or 0

        for ev in conn.execute(
            "SELECT kind, name, status, data_json FROM events WHERE run_id=? ORDER BY seq", (run_id,)
        ):
            kind = ev["kind"]
            facts.events_by_kind[kind] += 1
            data = json.loads(ev["data_json"] or "{}")
            if kind == "plan.created":
                facts.stages = data.get("stages") or data.get("agents") or []
            elif kind == "agent.session":
                facts.sessions += 1
            elif kind == "sandbox.run":
                facts.sandbox_runs += 1
            elif kind == "tool.result":
                facts.tool_results[data.get("result") or ev["status"]] += 1
            elif kind == "map.fanout":
                facts.fanout_workers = max(facts.fanout_workers, int(data.get("workers") or 0))
            elif kind == "review.verdict":
                facts.review_verdicts.append(str(data.get("verdict")))
            elif kind == "capability.promote" and ev["status"] != "running":
                facts.capabilities_created.append(ev["name"])
            elif kind == "decision" and data.get("choice") == "reuse":
                facts.capabilities_reused.append(str(data.get("capability")))
            elif kind == "capability.repair":
                facts.repairs += 1
            elif kind == "team.question":
                facts.team_questions += 1
            elif kind == "team.blocker":
                facts.team_blockers += 1
            elif kind == "agent.salvaged":
                facts.salvaged_sessions += 1
    finally:
        conn.close()

    base = Path(runs_dir) / run_id
    answer_file = base / "outputs" / "ANSWER.md"
    if answer_file.exists():
        facts.answer = answer_file.read_text()
    ws = base / "workspace"
    if ws.is_dir():
        facts.workspace_files = sorted(
            str(p.relative_to(ws)) for p in ws.rglob("*")
            if p.is_file() and "prior" not in p.relative_to(ws).parts[:1]
        )
    return facts
