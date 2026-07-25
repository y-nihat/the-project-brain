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
CREATE TABLE IF NOT EXISTS capability_failures (
    name          TEXT NOT NULL,   -- the capability NEED key (may never have promoted)
    failure_class TEXT,
    reason        TEXT,
    at            TEXT NOT NULL
);
"""

# Columns added after the original schema; applied to pre-existing databases.
_MIGRATIONS = {"memory_mb": "INTEGER", "cpus": "REAL", "timeout_seconds": "INTEGER",
               "requires_credential": "TEXT", "credential_help": "TEXT",
               "validated_engine": "TEXT", "validated_at": "TEXT", "quarantined": "TEXT",
               "accepts": "TEXT", "emits": "TEXT", "side_effects": "TEXT"}


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
            validated_engine=r["validated_engine"],
            quarantined=r["quarantined"],
            accepts=json.loads(r["accepts"]) if r["accepts"] else [],
            emits=r["emits"] or "",
            side_effects=(r["side_effects"] or "none")
            if "side_effects" in r.keys() else "none",
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

    def stamp_validated(self, name: str, engine_version: str) -> None:
        """Record that this capability passed the current probe battery."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE capabilities SET validated_engine=?, validated_at=?, quarantined=NULL "
                "WHERE name=?",
                (engine_version, _dt.datetime.now(_dt.timezone.utc).isoformat(), name))
        self._sync_manifest(name, validated_engine=engine_version, quarantined=None)

    def quarantine(self, name: str, reason: str) -> None:
        """Mark a capability unsafe for reuse (kept for audit/repair, never selected)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE capabilities SET quarantined=?, validated_at=? WHERE name=?",
                (reason[:500], _dt.datetime.now(_dt.timezone.utc).isoformat(), name))
        self._sync_manifest(name, quarantined=reason[:500])

    def record_failure(self, name: str, reason: str, failure_class: str = "") -> None:
        """Scoped negative procedural memory: a capability NEED that could not be
        built/verified, recorded so a later run does not walk into the same dead
        end without new rationale. Keyed by need key — the capability may never
        have been promoted at all."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO capability_failures (name, failure_class, reason, at) "
                "VALUES (?,?,?,?)",
                (name, failure_class[:60], reason[:500],
                 _dt.datetime.now(_dt.timezone.utc).isoformat()))

    def last_failures(self) -> dict[str, dict]:
        """Most recent recorded failure per need key: {name: {class, reason, at}}."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, failure_class, reason, MAX(at) AS at FROM capability_failures "
                "GROUP BY name").fetchall()
        return {r["name"]: {"class": r["failure_class"] or "", "reason": r["reason"] or "",
                            "at": r["at"]} for r in rows}

    def _sync_manifest(self, name: str, **fields) -> None:
        """Keep the human-facing manifest in agreement with the database — the
        two audit surfaces must never disagree about whether a capability is
        trusted (observed: db said quarantined, manifest said null)."""
        mf = self.store_dir / name / "manifest.json"
        if not mf.is_file():
            return
        try:
            data = json.loads(mf.read_text())
            data.update(fields)
            mf.write_text(json.dumps(data, indent=2))
        except (OSError, ValueError):
            pass  # the DB stays authoritative; a corrupt manifest must not break runs

    def add(self, cap: Capability, build_context: Optional[dict[str, str]] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO capabilities "
                "(name, version, description, image_tag, command, input_schema, env_map, network, "
                " tags, verified, created_at, memory_mb, cpus, timeout_seconds, requires_credential, "
                " credential_help, validated_engine, validated_at, quarantined, accepts, emits, "
                " side_effects) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    cap.validated_engine,
                    _dt.datetime.now(_dt.timezone.utc).isoformat() if cap.validated_engine else None,
                    cap.quarantined,
                    json.dumps(cap.accepts) if cap.accepts else None,
                    cap.emits or None,
                    cap.side_effects or "none",
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


def qualified_catalog(registry: Registry, epoch: str,
                      reuse_counts: Optional[dict[str, int]] = None) -> dict[str, str]:
    """The planner's known-capability view, QUALIFIED by trust and availability
    state — catalog hygiene BEFORE planning, not after (a quarantined tool with
    a task-shaped description was observed anchoring the initial plan, then
    getting quarantined only at acquisition time, after the strategy was set).

      * quarantined         -> excluded (unsafe memory must not advertise itself);
      * stale contract      -> labeled (usable only after revalidation/re-authoring);
      * previously failed   -> the need key is listed with its failure so naming
                               it again is a deliberate act, not amnesia;
      * descriptions        -> normalized (task residue stripped) defensively;
      * earned reuse        -> stated, when known. The planner chose from
                               descriptions alone and could not tell a tool
                               proven across dozens of runs from one that has
                               never been picked since the day it was built.
                               Reuse is the library's whole purpose, so what
                               has actually been reused is evidence the
                               planner should have.
    """
    from .acquisition import generalize_description  # lazy: avoid import cycle

    known: dict[str, str] = {}
    for cap in registry.all():
        if cap.quarantined:
            continue
        desc = generalize_description(cap.description)
        if cap.validated_engine != epoch:
            desc += (" [stale contract: revalidated before any reuse; may be "
                     "quarantined and re-authored]")
        if reuse_counts is not None:
            n = reuse_counts.get(cap.name, 0)
            desc += (f" [reused in {n} prior run(s)]" if n
                     else " [never reused since it was built]")
        known[cap.name] = desc
    for name, failure in registry.last_failures().items():
        if name in known:
            continue
        known[name] = (f"(UNAVAILABLE — a previous attempt to build this failed: "
                       f"{failure['class'] or 'unclassified'}: {failure['reason'][:160]}. "
                       "Re-selecting it requires a materially different approach or new "
                       "rationale; otherwise choose another method.)")
    return known
