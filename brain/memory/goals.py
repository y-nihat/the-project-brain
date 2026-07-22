"""Standing goals: aspirations that outlive a single run.

A run's objective is consumed and closed when the run ends; an ASPIRATION the
user stated ("achieve a high score", "get latency under X") outlives it. This
ledger is the engine's memory of such goals: what is being pursued, how its
true value is observed, and the full trajectory of attempts with their
realized outcomes — so a later run (or the goal-pursuit loop) knows where the
project stands instead of the human having to restate it.

Generality: a goal is {metric, direction, target?, measured_by}; nothing here
names a task, service, or tool. Values are stored as text and compared
numerically when both parse — otherwise comparison is left to the judgment
layer (brain/goals.py).
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
import threading
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'maximize',
    target TEXT NOT NULL DEFAULT '',
    measured_by TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goal_attempts (
    goal_id INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL DEFAULT 'unknown',
    diagnosis TEXT NOT NULL DEFAULT '',
    next_step TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL
);
"""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _num(value) -> Optional[float]:
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


class GoalLedger:
    def __init__(self, db_path: str | Path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    # ---- goals ---------------------------------------------------------- #
    def declare(self, metric: str, direction: str = "maximize", target: str = "",
                measured_by: str = "") -> int:
        direction = direction if direction in ("maximize", "minimize", "reach") else "maximize"
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO goals (metric, direction, target, measured_by, status, created) "
                "VALUES (?,?,?,?, 'open', ?)",
                (str(metric)[:400], direction, str(target)[:100],
                 str(measured_by)[:400], _now()))
            return int(cur.lastrowid)

    def get(self, goal_id) -> Optional[dict]:
        try:
            gid = int(goal_id)
        except (TypeError, ValueError):
            return None
        row = self._conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
        return dict(row) if row else None

    def mark(self, goal_id: int, status: str) -> None:
        if status not in ("open", "achieved", "abandoned"):
            return
        with self._lock, self._conn:
            self._conn.execute("UPDATE goals SET status=? WHERE id=?", (status, int(goal_id)))

    def open_goals(self, limit: int = 5) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM goals WHERE status='open' ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()
        out = []
        for r in rows:
            g = dict(r)
            g["attempts"] = self.attempts(g["id"])
            g["best"] = self.best_value(g)
            out.append(g)
        return out

    # ---- attempts ------------------------------------------------------- #
    def record_attempt(self, goal_id: int, run_id: str, value: str = "",
                       verdict: str = "unknown", diagnosis: str = "",
                       next_step: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO goal_attempts (goal_id, run_id, value, verdict, diagnosis, "
                "next_step, ts) VALUES (?,?,?,?,?,?,?)",
                (int(goal_id), str(run_id), str(value)[:100], str(verdict)[:20],
                 str(diagnosis)[:1500], str(next_step)[:1500], _now()))

    def attempts(self, goal_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM goal_attempts WHERE goal_id=? ORDER BY ts", (int(goal_id),))
        return [dict(r) for r in rows.fetchall()]

    def best_value(self, goal: dict) -> Optional[str]:
        """The direction-best MEASURED value across attempts (numeric compare
        when values parse; None when no attempt carries a parseable value).
        'reach' = closest to target."""
        vals = [(a["value"], _num(a["value"])) for a in self.attempts(goal["id"])
                if a.get("value")]
        numeric = [(raw, n) for raw, n in vals if n is not None]
        if not numeric:
            return vals[-1][0] if vals else None
        direction = goal.get("direction")
        if direction == "reach":
            t = _num(goal.get("target"))
            if t is None:                      # no numeric target — latest is the best read
                return vals[-1][0]
            return min(numeric, key=lambda p: abs(p[1] - t))[0]
        pick = min if direction == "minimize" else max
        return pick(numeric, key=lambda p: p[1])[0]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


def goals_block(ledger: "GoalLedger") -> str:
    """Planner-facing summary of open standing goals — so a new objective that
    continues one can reference it (aspiration.goal_ref) instead of redeclaring."""
    goals = ledger.open_goals()
    if not goals:
        return ""
    lines = ["STANDING GOALS (open — if this objective continues one, set "
             "aspiration.goal_ref to its id):"]
    for g in goals:
        n = len(g["attempts"])
        last = g["attempts"][-1] if n else None
        lines.append(
            f"- goal #{g['id']}: {g['metric']} [{g['direction']}"
            + (f" -> {g['target']}" if g["target"] else "")
            + f"] — {n} attempt(s), best measured: {g['best'] or '(none)'}"
            + (f", last verdict: {last['verdict']}" if last else ""))
    return "\n".join(lines)
