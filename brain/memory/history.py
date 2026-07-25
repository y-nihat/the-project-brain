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


# How far back relevance may look. The window exists so the scan stays bounded
# as the corpus grows, not to define what the brain may remember: at 50 it was
# already smaller than the recorded history, so the system silently forgot more
# than half of what it had done.
CANDIDATE_WINDOW = 500


def relevant_runs(db_path: str | Path, objective: str, limit: int = 3) -> list[dict]:
    """Prior runs ranked by token overlap with the new objective (best first).

    Overlap is normalized (Jaccard). Raw intersection size rewarded LENGTH:
    a long prior objective shares more tokens with anything by accident, so
    the biggest previous task tended to win regardless of similarity — and the
    winner's workspace is seeded into the new run, so a bad pick is not merely
    unhelpful, it puts the wrong files in front of the agents.

    Still deliberately dependency-free lexical matching. Semantic retrieval is
    a real upgrade but there is nothing to judge it against yet: no signal
    records whether a seeded run was actually USED, so a swap could not be
    shown to be an improvement.
    """
    conn = _connect_ro(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT run_id, objective, status, outcome, started_at FROM runs "
            "ORDER BY started_at DESC LIMIT ?", (CANDIDATE_WINDOW,)
        ).fetchall()
    finally:
        conn.close()

    want = _tokens(objective)
    if not want:
        return []
    scored = []
    for r in rows:
        obj_tokens = _tokens(r["objective"])
        if not want & obj_tokens:
            continue  # require at least some objective-level overlap
        have = obj_tokens | _tokens((r["outcome"] or "")[:2000])
        union = want | have
        score = len(want & have) / len(union) if union else 0.0
        # Delivered outcomes and recency break ties; neither may outweigh
        # similarity, which a flat +1 on a normalized score would.
        scored.append((score, r["status"] == "succeeded",
                       r["started_at"] or "", dict(r)))
    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    return [t[3] for t in scored[:limit]]


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
        # A prior answer is NARRATION, not evidence: injecting it unlabeled let
        # a new run repeat a prior conclusion without the observations that
        # once supported it (and would amplify a wrong one just as readily).
        lines.append(
            "its final answer (EXCERPT — prior narration, NOT evidence: treat as a "
            "hypothesis; any load-bearing claim reused from it must be re-established "
            "from sources observed in the CURRENT run, or delivered explicitly as "
            "unverified): " + answer[:600].replace("\n", " "))
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
