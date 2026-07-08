"""Capability registry: the growing, reusable capability library.

Backed by SQLite for lookup plus a ``capabilities_store/<name>/`` directory that
holds a human-readable manifest and the build context (Dockerfile + sources) so
every promoted capability is reproducible.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .models import Capability

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capabilities (
    name         TEXT PRIMARY KEY,
    version      TEXT,
    description  TEXT,
    image_tag    TEXT,
    command      TEXT,   -- json list
    input_schema TEXT,   -- json
    env_map      TEXT,   -- json
    network      TEXT,
    tags         TEXT,   -- json list
    verified     INTEGER,
    created_at   TEXT,
    memory_mb    INTEGER,
    cpus         REAL,
    timeout_seconds INTEGER
);
"""

# Columns added after the original schema; applied to pre-existing databases.
_MIGRATIONS = {"memory_mb": "INTEGER", "cpus": "REAL", "timeout_seconds": "INTEGER",
               "requires_credential": "TEXT", "credential_help": "TEXT"}


def _migrate(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


class Registry:
    def __init__(self, db_path: str | Path, store_dir: str | Path):
        self.db_path = Path(db_path)
        self.store_dir = Path(store_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)
            _migrate(self._conn, "capabilities", _MIGRATIONS)

    def _row_to_cap(self, r: sqlite3.Row) -> Capability:
        return Capability(
            name=r["name"],
            description=r["description"],
            image_tag=r["image_tag"],
            command=json.loads(r["command"]),
            input_schema=json.loads(r["input_schema"]),
            env_map=json.loads(r["env_map"]),
            network=r["network"],
            version=r["version"],
            tags=json.loads(r["tags"]),
            verified=bool(r["verified"]),
            memory_mb=r["memory_mb"],
            cpus=r["cpus"],
            timeout_seconds=r["timeout_seconds"],
            requires_credential=r["requires_credential"],
            credential_help=r["credential_help"],
        )

    def get(self, name: str) -> Optional[Capability]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM capabilities WHERE name=?", (name,)
            ).fetchone()
        return self._row_to_cap(row) if row else None

    def all(self) -> list[Capability]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM capabilities ORDER BY name").fetchall()
        return [self._row_to_cap(r) for r in rows]

    def add(self, cap: Capability, build_context: Optional[dict[str, str]] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO capabilities "
                "(name, version, description, image_tag, command, input_schema, env_map, network, "
                " tags, verified, created_at, memory_mb, cpus, timeout_seconds, requires_credential, "
                " credential_help) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cap.name,
                    cap.version,
                    cap.description,
                    cap.image_tag,
                    json.dumps(cap.command),
                    json.dumps(cap.input_schema),
                    json.dumps(cap.env_map),
                    cap.network,
                    json.dumps(cap.tags),
                    int(cap.verified),
                    _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    cap.memory_mb,
                    cap.cpus,
                    cap.timeout_seconds,
                    cap.requires_credential,
                    cap.credential_help,
                ),
            )
        # Persist a manifest + build context for reproducibility/audit.
        capdir = self.store_dir / cap.name
        capdir.mkdir(parents=True, exist_ok=True)
        (capdir / "manifest.json").write_text(json.dumps(dataclasses.asdict(cap), indent=2))
        if build_context:
            bc = capdir / "build"
            bc.mkdir(parents=True, exist_ok=True)
            for filename, content in build_context.items():
                (bc / filename).write_text(content)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
