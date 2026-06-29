"""Shared state ("blackboard") for a run.

The flat orchestrator owns context: agents don't share memory directly. The
orchestrator writes task results here and feeds relevant slices into each
agent's prompt. Backed by SQLite, scoped by session, so a run's state survives
the process for later inspection/learning.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blackboard (
    session TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT,        -- json
    ts      TEXT NOT NULL,
    PRIMARY KEY (session, key)
);
"""


class Blackboard:
    def __init__(self, db_path: str | Path, session: str):
        self.db_path = Path(db_path)
        self.session = session
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def put(self, key: str, value: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO blackboard (session, key, value, ts) VALUES (?,?,?,?)",
                (self.session, key, json.dumps(value), _dt.datetime.now(_dt.timezone.utc).isoformat()),
            )

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM blackboard WHERE session=? AND key=?", (self.session, key)
            ).fetchone()
        return json.loads(row["value"]) if row else default

    def items(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM blackboard WHERE session=?", (self.session,)
            ).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
