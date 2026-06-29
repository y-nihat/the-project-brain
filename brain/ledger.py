"""Resource ledger: a SQLite record of every container the brain creates.

The ledger is the accounting source of truth for the "no sprawl, no leaks"
constraint. Every sandbox container is recorded on start and updated on finish;
``find_leaks()`` surfaces anything still marked running.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS containers (
    id           TEXT PRIMARY KEY,
    session      TEXT NOT NULL,
    capability   TEXT,
    image        TEXT,
    command      TEXT,
    mem          TEXT,
    cpus         REAL,
    pids         INTEGER,
    network      TEXT,
    ttl          INTEGER,
    status       TEXT NOT NULL,          -- running | finished | failed | reaped
    exit_code    INTEGER,
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);
"""


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class Ledger:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def record_start(
        self,
        *,
        id: str,
        session: str,
        capability: Optional[str],
        image: str,
        command: Any,
        policy: Any,
    ) -> None:
        cmd = command if isinstance(command, str) else json.dumps(command)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO containers "
                "(id, session, capability, image, command, mem, cpus, pids, network, ttl, status, started_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, 'running', ?)",
                (
                    id,
                    session,
                    capability,
                    image,
                    cmd,
                    policy.mem,
                    policy.cpus,
                    policy.pids,
                    policy.network,
                    policy.ttl_seconds,
                    _utcnow(),
                ),
            )

    def record_finish(self, id: str, *, exit_code: Optional[int], status: str = "finished") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE containers SET status=?, exit_code=?, finished_at=? WHERE id=?",
                (status, exit_code, _utcnow(), id),
            )

    def find_leaks(self, session: Optional[str] = None) -> list[dict]:
        """Rows still marked 'running' — i.e. containers we never confirmed gone."""
        query = "SELECT * FROM containers WHERE status='running'"
        params: tuple = ()
        if session is not None:
            query += " AND session=?"
            params = (session,)
        with self._lock:
            return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def all(self, session: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM containers"
        params: tuple = ()
        if session is not None:
            query += " WHERE session=?"
            params = (session,)
        query += " ORDER BY started_at"
        with self._lock:
            return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
