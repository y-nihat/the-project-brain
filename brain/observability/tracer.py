"""The tracer: records each run as a trace of nested spans + point events.

Design:
  * A *span* is one event row written on enter (status="running") and updated on
    exit with duration + final status/data. Writing on enter keeps `seq`
    chronological and guarantees a parent row exists before its children.
  * A *point event* is a single row.
  * Nesting (`parent_id`) is tracked through a ``contextvars`` span stack, so it
    stays correct across ``async`` code and ``asyncio.to_thread`` hops.
  * Run-level rollups (containers, tokens, cost, approvals) are DERIVED from the
    event rows at finish time — no extra plumbing through the call graph.

``NullTracer`` mirrors the whole interface as no-ops.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime as _dt
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

_current_span: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "brain_current_span", default=None
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    objective      TEXT,
    status         TEXT,          -- running | succeeded | failed | blocked
    model          TEXT,
    engine_version TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    n_events       INTEGER,
    n_containers   INTEGER,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    cost_usd       REAL,
    n_approvals    INTEGER,
    duration_ms    INTEGER,
    outcome        TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    parent_id   TEXT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    name        TEXT,
    status      TEXT,
    duration_ms INTEGER,
    data_json   TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, seq);
"""


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


_SLUG_STOP = {
    "a", "an", "the", "that", "this", "it", "is", "are", "on", "in", "at", "of",
    "to", "for", "with", "and", "or", "be", "do", "not", "no", "your", "you",
    "my", "our", "me", "i", "we", "using", "use",
}


def _slug(text: str, max_len: int = 40) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _SLUG_STOP]
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > max_len:
            break
        out = f"{out}-{w}" if out else w
    return out or "task"


def _run_id(objective: str) -> str:
    """Readable, sortable run id: run-<timestamp>-<objective-slug>-<uniq>."""
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{_slug(objective)}-{uuid.uuid4().hex[:4]}"


class Span:
    """Handle yielded by ``tracer.span(...)``; collects data set during the span."""

    def __init__(self, event_id: Optional[str], data: dict):
        self.id = event_id
        self.data = dict(data)
        self.status = "ok"

    def set(self, **data: Any) -> "Span":
        self.data.update(data)
        return self


# --------------------------------------------------------------------------- #
# No-op implementation
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _null_span():
    yield Span(None, {})


class _NullRun:
    run_id = None

    def span(self, *_a, **_k):
        return _null_span()

    def event(self, *_a, **_k):
        pass

    def artifact_path(self, *_parts):
        import tempfile

        return Path(tempfile.mkdtemp())

    def finish(self, *_a, **_k):
        pass


class NullTracer:
    """Full tracer interface as no-ops."""

    def start_run(self, *_a, **_k) -> _NullRun:
        return _NullRun()

    def span(self, *_a, **_k):
        return _null_span()

    def event(self, *_a, **_k):
        pass

    def artifact_path(self, *_parts):
        import tempfile

        return Path(tempfile.mkdtemp())


# --------------------------------------------------------------------------- #
# Real tracer
# --------------------------------------------------------------------------- #
class Tracer:
    def __init__(self, db_path: str | Path, runs_dir: str | Path, *, engine_version: str = "0.0.0"):
        self.db_path = Path(db_path)
        self.runs_dir = Path(runs_dir)
        self.engine_version = engine_version
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)
        self._run: Optional[Run] = None
        self._listeners: list = []

    def add_listener(self, fn) -> None:
        """Register a callback invoked with each event record as it is written.

        For live consumers (e.g. the REPL's progress stream). Best-effort: a
        listener that raises never affects recording or the run.
        """
        self._listeners.append(fn)

    # -- run lifecycle ---------------------------------------------------- #
    def start_run(self, objective: str, *, model: Optional[str] = None) -> "Run":
        run_id = _run_id(objective)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO runs (run_id, objective, status, model, engine_version, started_at) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, objective, "running", model, self.engine_version, _utcnow()),
            )
        run = Run(self, run_id, objective)
        run._dir.mkdir(parents=True, exist_ok=True)
        run._jsonl = run._dir / "trace.jsonl"
        self._run = run
        # contextvars are reset per run so nesting starts clean.
        _current_span.set(None)
        run.event("run", "start", status="running", objective=objective, model=model)
        return run

    # -- delegation to current run --------------------------------------- #
    def span(self, kind: str, name: str, **data: Any):
        if self._run is None:
            return _null_span()
        return self._run.span(kind, name, **data)

    def event(self, kind: str, name: str, status: str = "info", **data: Any) -> None:
        if self._run is not None:
            self._run.event(kind, name, status=status, **data)

    def artifact_path(self, *parts: str) -> Path:
        """Directory for this run's output artifacts (created tool scripts, etc.)."""
        if self._run is None:
            import tempfile

            return Path(tempfile.mkdtemp())
        return self._run.artifact_path(*parts)

    def abort_stale_runs(self) -> int:
        """Mark any 'running' runs as aborted — a previous process died mid-run
        (only one session runs at a time, so a pre-existing 'running' row is a lie)."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE runs SET status='aborted', finished_at=? WHERE status='running'",
                (_utcnow(),),
            )
            return cur.rowcount

    # -- low-level writes ------------------------------------------------- #
    def _insert(self, run_id: str, *, kind: str, name: str, status: str, parent_id: Optional[str], data: dict) -> str:
        eid = _new_id("ev")
        ts = _utcnow()
        with self._lock, self._conn:
            seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM events WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            self._conn.execute(
                "INSERT INTO events (event_id, run_id, seq, parent_id, ts, kind, name, status, duration_ms, data_json, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (eid, run_id, seq, parent_id, ts, kind, name, status, None, json.dumps(data or {}), None),
            )
        self._jsonl(run_id, {"phase": "start" if kind_is_span(status) else "event", "event_id": eid,
                             "seq": seq, "parent_id": parent_id, "ts": ts, "kind": kind, "name": name,
                             "status": status, "data": data or {}})
        return eid

    def _update(self, eid: str, *, status: str, duration_ms: int, data: dict, error: Optional[str],
                kind: str = "", name: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE events SET status=?, duration_ms=?, data_json=?, error=? WHERE event_id=?",
                (status, duration_ms, json.dumps(data or {}), error, eid),
            )
        self._jsonl_run({"phase": "end", "event_id": eid, "kind": kind, "name": name,
                         "status": status, "duration_ms": duration_ms,
                         "data": data or {}, "error": error})

    def _jsonl(self, run_id: str, record: dict) -> None:
        self._jsonl_run(record)

    def _jsonl_run(self, record: dict) -> None:
        run = self._run
        if run is None or run._jsonl is None:
            return
        try:
            with open(run._jsonl, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass
        for fn in self._listeners:
            try:
                fn(record)
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def kind_is_span(status: str) -> bool:
    return status == "running"


class Run:
    def __init__(self, tracer: Tracer, run_id: str, objective: str):
        self._t = tracer
        self.run_id = run_id
        self.objective = objective
        self._dir = tracer.runs_dir / run_id
        self._jsonl: Optional[Path] = None
        self._start = time.monotonic()

    def event(self, kind: str, name: str, status: str = "info", **data: Any) -> str:
        return self._t._insert(self.run_id, kind=kind, name=name, status=status,
                               parent_id=_current_span.get(), data=data)

    @contextlib.contextmanager
    def span(self, kind: str, name: str, **data: Any):
        parent = _current_span.get()
        eid = self._t._insert(self.run_id, kind=kind, name=name, status="running",
                              parent_id=parent, data=data)
        token = _current_span.set(eid)
        sp = Span(eid, data)
        t0 = time.monotonic()
        try:
            yield sp
        except Exception as e:
            self._t._update(eid, kind=kind, name=name, status="failed",
                            duration_ms=int((time.monotonic() - t0) * 1000),
                            data=sp.data, error=repr(e))
            raise
        else:
            self._t._update(eid, kind=kind, name=name, status=sp.status,
                            duration_ms=int((time.monotonic() - t0) * 1000),
                            data=sp.data, error=None)
        finally:
            _current_span.reset(token)

    def artifact_path(self, *parts: str) -> Path:
        """`runs/<run_id>/artifacts/<parts...>` (created on demand)."""
        p = self._dir / "artifacts"
        for part in parts:
            p = p / str(part)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def finish(self, status: str = "succeeded", outcome: Optional[str] = None) -> None:
        conn = self._t._conn
        with self._t._lock, conn:
            agg = conn.execute(
                """SELECT
                     COUNT(*) AS n_events,
                     SUM(CASE WHEN kind='sandbox.run' THEN 1 ELSE 0 END) AS n_containers,
                     SUM(CASE WHEN kind='approval.requested' THEN 1 ELSE 0 END) AS n_approvals,
                     COALESCE(SUM(COALESCE(json_extract(data_json,'$.tokens_in'),0)),0) AS tin,
                     COALESCE(SUM(COALESCE(json_extract(data_json,'$.tokens_out'),0)),0) AS tout,
                     COALESCE(SUM(COALESCE(json_extract(data_json,'$.cost_usd'),0)),0) AS cost
                   FROM events WHERE run_id=?""",
                (self.run_id,),
            ).fetchone()
            conn.execute(
                "UPDATE runs SET status=?, finished_at=?, n_events=?, n_containers=?, n_approvals=?, "
                "tokens_in=?, tokens_out=?, cost_usd=?, duration_ms=?, outcome=? WHERE run_id=?",
                (
                    status, _utcnow(), agg["n_events"], agg["n_containers"] or 0, agg["n_approvals"] or 0,
                    agg["tin"] or 0, agg["tout"] or 0, float(agg["cost"] or 0.0),
                    int((time.monotonic() - self._start) * 1000),
                    (outcome or "")[:20000], self.run_id,
                ),
            )
        # Export the human-readable timeline + keep data/runs navigable.
        try:
            from .view import refresh_index, render_run

            (self._dir / "trace.md").write_text(render_run(self._t.db_path, self.run_id))
            refresh_index(self._t.db_path, self._t.runs_dir)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Instrumented agent session
# --------------------------------------------------------------------------- #
class AgentStallError(RuntimeError):
    """An agent session stopped making progress or blew its total budget."""


async def _iter_with_watchdog(ait, stall_s: float, total_s: float, label: str):
    """Yield from an async iterator under a watchdog.

    Raises :class:`AgentStallError` when no item arrives within ``stall_s``
    (a stall — e.g. a hung SDK call or a rate-limited backend) or when the whole
    iteration exceeds ``total_s``. The underlying iterator is closed on exit.
    """
    start = time.monotonic()
    it = ait.__aiter__()
    try:
        while True:
            elapsed = time.monotonic() - start
            remaining = total_s - elapsed
            if remaining <= 0:
                raise AgentStallError(
                    f"{label} exceeded its total session budget ({int(total_s)}s)"
                )
            wait = min(stall_s, remaining)
            try:
                msg = await asyncio.wait_for(it.__anext__(), timeout=wait)
            except asyncio.TimeoutError:
                if wait < stall_s:  # the wait was budget-limited, not a stall
                    raise AgentStallError(
                        f"{label} exceeded its total session budget ({int(total_s)}s)"
                    ) from None
                raise AgentStallError(
                    f"{label}: no activity for {int(wait)}s "
                    f"(stall watchdog; {int(elapsed)}s elapsed in session)"
                ) from None
            except StopAsyncIteration:
                return
            yield msg
    finally:
        aclose = getattr(it, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass


async def trace_query(tracer, name: str, prompt, options, *,
                      stall_timeout: Optional[float] = None,
                      total_timeout: Optional[float] = None) -> str:
    """Run an SDK ``query()`` inside an ``agent.session`` span, emitting a
    ``agent.tool_call`` event per tool use and capturing the final text + usage —
    under a watchdog so a stalled session fails loudly instead of hanging.

    Token/cost field names are read defensively (``getattr``/dict) so we tolerate
    SDK version differences.
    """
    from claude_agent_sdk import query  # lazy: keep module importable without the SDK

    from .. import config

    stall = stall_timeout if stall_timeout is not None else config.AGENT_STALL_TIMEOUT
    total = total_timeout if total_timeout is not None else config.AGENT_TOTAL_TIMEOUT

    final = ""
    with tracer.span("agent.session", name) as sp:
        stream = _iter_with_watchdog(
            query(prompt=prompt, options=options), stall, total, f"agent '{name}'"
        )
        async for message in stream:
            _maybe_emit_tool_calls(tracer, message)
            _maybe_capture_result(sp, message)
            text = getattr(message, "result", None)
            if text:
                final = text
    return final or ""


def _maybe_emit_tool_calls(tracer, message) -> None:
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return
    for block in content:
        # ToolUseBlock has name + input; detect structurally to avoid import coupling.
        bname = getattr(block, "name", None)
        binput = getattr(block, "input", None)
        if bname is not None and binput is not None:
            keys = list(binput.keys()) if isinstance(binput, dict) else None
            tracer.event("agent.tool_call", str(bname), status="ok", input_keys=keys)


def _maybe_capture_result(sp, message) -> None:
    # Only ResultMessage carries usage/cost; detect by presence of `usage`/`result`.
    usage = getattr(message, "usage", None)
    cost = getattr(message, "total_cost_usd", None)
    if cost is None:
        cost = getattr(message, "cost_usd", None)
    tin = tout = None
    if isinstance(usage, dict):
        tin = usage.get("input_tokens")
        tout = usage.get("output_tokens")
    elif usage is not None:
        tin = getattr(usage, "input_tokens", None)
        tout = getattr(usage, "output_tokens", None)
    updates = {}
    if tin is not None:
        updates["tokens_in"] = tin
    if tout is not None:
        updates["tokens_out"] = tout
    if cost is not None:
        updates["cost_usd"] = cost
    turns = getattr(message, "num_turns", None)
    if turns is not None:
        updates["turns"] = turns
    if updates:
        sp.set(**updates)
