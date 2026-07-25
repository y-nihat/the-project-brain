"""What a turn — and a whole conversation — actually cost.

The numbers were always recorded and only ever printed on the one-shot path, so
the interactive path, the one a conversation lives in, reported nothing. A
system whose runs span cents to double-digit dollars cannot ask a person to
converse with it blind.

Best-effort throughout: accounting must never break delivery.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


def _connect_ro(db_path: str | Path) -> Optional[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def run_cost(db_path: str | Path, run_id: str) -> Optional[dict]:
    conn = _connect_ro(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT cost_usd, duration_ms, tokens_in, tokens_out FROM runs WHERE run_id=?",
            (run_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row or row["cost_usd"] is None:
        return None
    return dict(row)


def conversation_cost(db_path: str | Path, conversation_id: str) -> dict:
    """Spend and turn count for one conversation, so far."""
    conn = _connect_ro(db_path)
    if conn is None:
        return {"cost_usd": 0.0, "runs": 0, "turns": 0}
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) cost, COUNT(*) runs, "
            "       SUM(COALESCE(origin,'user')='user') turns "
            "FROM runs WHERE conversation_id=?", (conversation_id,)).fetchone()
        return {"cost_usd": row["cost"] or 0.0, "runs": row["runs"] or 0,
                "turns": row["turns"] or 0}
    except sqlite3.Error:
        return {"cost_usd": 0.0, "runs": 0, "turns": 0}
    finally:
        conn.close()


def run_cost_line(db_path: str | Path, run_id: str) -> str:
    row = run_cost(db_path, run_id)
    if row is None:
        return ""
    mins = (row["duration_ms"] or 0) / 60000.0
    return (f"[cost]       ${row['cost_usd']:.2f} · {mins:.1f} min · "
            f"{(row['tokens_in'] or 0):,} in / {(row['tokens_out'] or 0):,} out tokens")


def turn_cost_line(db_path: str | Path, run_id: str, conversation_id: str) -> str:
    """One compact line for an interactive turn: this turn, and the
    conversation so far — the two numbers a person actually decides on."""
    row = run_cost(db_path, run_id)
    total = conversation_cost(db_path, conversation_id)
    if row is None and not total["runs"]:
        return ""
    mins = ((row or {}).get("duration_ms") or 0) / 60000.0
    this = f"${((row or {}).get('cost_usd') or 0.0):.2f} · {mins:.1f} min"
    return (f"[cost] this turn {this}   ·   conversation so far "
            f"${total['cost_usd']:.2f} over {total['turns']} turn(s)")
