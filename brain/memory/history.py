"""Cross-run episodic memory (v1): let a new run know what the brain did before.

Relevance is simple token overlap between the new objective and past runs'
objectives + outcomes — an honest, dependency-free v1 (semantic retrieval is a
later upgrade). All reads are tolerant of a missing/empty database (first run).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Optional

_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "using", "use",
    "must", "should", "never", "each", "any", "all", "your", "you", "are", "was",
    "not", "only", "also", "one", "two", "three", "produce", "build", "create",
    "make", "task", "step", "user",
}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def _connect_ro(db_path: str | Path) -> Optional[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM runs LIMIT 1")
        return conn
    except Exception:  # noqa: BLE001  (no db yet / no runs table)
        return None


def relevant_runs(db_path: str | Path, objective: str, limit: int = 3) -> list[dict]:
    """Prior runs ranked by token overlap with the new objective (best first)."""
    conn = _connect_ro(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT run_id, objective, status, outcome, started_at FROM runs "
            "ORDER BY started_at DESC LIMIT 50"
        ).fetchall()
    finally:
        conn.close()

    want = _tokens(objective)
    scored = []
    for r in rows:
        have = _tokens(r["objective"]) | _tokens((r["outcome"] or "")[:2000])
        score = len(want & have)
        if r["status"] == "succeeded":
            score += 1  # prefer runs that actually delivered
        if len(want & _tokens(r["objective"])) == 0:
            continue  # require at least some objective-level overlap
        scored.append((score, dict(r)))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [r for _, r in scored[:limit]]


def _workspace_files(runs_dir: str | Path, run_id: str, cap: int = 20) -> list[str]:
    ws = Path(runs_dir) / run_id / "workspace"
    if not ws.is_dir():
        return []
    files = sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file())
    return files[:cap]


def _final_answer(db_path: str | Path, runs_dir: str | Path, run: dict) -> str:
    answer_file = Path(runs_dir) / run["run_id"] / "outputs" / "ANSWER.md"
    if answer_file.exists():
        return answer_file.read_text()
    return run.get("outcome") or ""


def _capabilities_created(db_path: str | Path, run_id: str) -> list[str]:
    conn = _connect_ro(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT name FROM events WHERE run_id=? AND kind='capability.promote'",
            (run_id,),
        ).fetchall()
        return [r["name"] for r in rows]
    finally:
        conn.close()


def run_context(db_path: str | Path, runs_dir: str | Path, run: dict, max_chars: int = 1500) -> str:
    """A compact, bounded context block describing one prior run."""
    lines = [
        f"### prior run {run['run_id']} [{run['status']}]",
        f"objective: {run['objective']}",
    ]
    caps = _capabilities_created(db_path, run["run_id"])
    if caps:
        lines.append(f"capabilities it created (now reusable): {', '.join(caps)}")
    files = _workspace_files(runs_dir, run["run_id"])
    if files:
        lines.append("files it produced (seedable into the current workspace): " + ", ".join(files))
    answer = _final_answer(db_path, runs_dir, run).strip()
    if answer:
        lines.append("its final answer (excerpt): " + answer[:600].replace("\n", " "))
    return "\n".join(lines)[:max_chars]


def history_block(
    db_path: str | Path,
    runs_dir: str | Path,
    objective: str,
    max_runs: int = 3,
    max_chars: int = 4000,
) -> tuple[Optional[str], Optional[str]]:
    """Assembled history context for a new objective.

    Returns ``(text, seed_run_id)`` — ``seed_run_id`` is the most relevant prior
    run that has workspace files worth copying into the new run's workspace;
    both are None when there is no relevant history.
    """
    runs = relevant_runs(db_path, objective, limit=max_runs)
    if not runs:
        return (None, None)

    blocks, total = [], 0
    for run in runs:
        block = run_context(db_path, runs_dir, run)
        if total + len(block) > max_chars:
            break
        blocks.append(block)
        total += len(block)
    if not blocks:
        return (None, None)

    seed = next(
        (r["run_id"] for r in runs if _workspace_files(runs_dir, r["run_id"], cap=1)),
        None,
    )
    return ("\n\n".join(blocks), seed)
