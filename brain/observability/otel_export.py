"""Export recorded runs to an OTLP collector (e.g. Arize Phoenix).

The brain's SQLite trace store remains the source of truth; this module replays
any recorded run as OpenTelemetry spans so it can be inspected interactively
(waterfall tree, timings, tokens/cost) in any OTLP-compatible viewer.

Spans carry OpenInference `openinference.span.kind` attributes so Phoenix
renders agents/tools/chains natively.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from pathlib import Path
from typing import Optional

# Our event kinds -> OpenInference span kinds (how Phoenix renders them).
_SPAN_KINDS = {
    "agent.session": "AGENT",
    "agent.tool_call": "TOOL",
    "sandbox.run": "TOOL",
    "image.build": "TOOL",
}
_DEFAULT_KIND = "CHAIN"

_ERROR_STATUSES = {"failed", "denied", "blocked"}


def _ns(ts_iso: str) -> int:
    return int(_dt.datetime.fromisoformat(ts_iso).timestamp() * 1_000_000_000)


def _attr_value(v):
    if isinstance(v, (str, bool, int, float)):
        return v if not isinstance(v, str) else v[:2000]
    return json.dumps(v)[:2000]


def _attributes(kind: str, name: str, run_id: str, data: dict) -> dict:
    attrs = {
        "openinference.span.kind": _SPAN_KINDS.get(kind, _DEFAULT_KIND),
        "brain.kind": kind,
        "brain.run_id": run_id,
    }
    for key, value in (data or {}).items():
        if value is None:
            continue
        attrs[f"brain.{key}"] = _attr_value(value)
    # Token/cost mapping so Phoenix aggregates usage.
    if kind == "agent.session":
        tin, tout = data.get("tokens_in"), data.get("tokens_out")
        if isinstance(tin, (int, float)):
            attrs["llm.token_count.prompt"] = int(tin)
        if isinstance(tout, (int, float)):
            attrs["llm.token_count.completion"] = int(tout)
        if isinstance(tin, (int, float)) and isinstance(tout, (int, float)):
            attrs["llm.token_count.total"] = int(tin) + int(tout)
    if kind == "agent.tool_call":
        attrs["tool.name"] = name
    return attrs


def export_run(db_path: str | Path, run_id: str, endpoint: str) -> int:
    """Replay one recorded run as an OTLP trace. Returns the span count.

    Raises RuntimeError if the collector doesn't acknowledge the export.
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.trace import Status, StatusCode, set_span_in_context

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise RuntimeError(f"run {run_id} not found")
        events = conn.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
    finally:
        conn.close()

    provider = TracerProvider(
        resource=Resource.create({"service.name": "the-project-brain"})
    )
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("brain.observability")

    # Root span: the run itself (one trace per run).
    run_start = _ns(run["started_at"])
    run_end = _ns(run["finished_at"]) if run["finished_at"] else None
    root = tracer.start_span(
        f"run: {(run['objective'] or run_id)[:80]}",
        start_time=run_start,
        attributes={
            "openinference.span.kind": "AGENT",
            "brain.run_id": run_id,
            "brain.status": run["status"] or "",
            "brain.model": run["model"] or "(default)",
            "brain.engine_version": run["engine_version"] or "",
            "brain.containers": run["n_containers"] or 0,
            "brain.approvals": run["n_approvals"] or 0,
            "llm.token_count.prompt": run["tokens_in"] or 0,
            "llm.token_count.completion": run["tokens_out"] or 0,
            "brain.cost_usd": float(run["cost_usd"] or 0.0),
            "brain.outcome": (run["outcome"] or "")[:2000],
        },
    )
    if run["status"] == "failed":
        root.set_status(Status(StatusCode.ERROR, "run failed"))

    spans = {None: root}
    order: list[str] = []
    ends: dict[str, int] = {}
    last_end = run_start

    for e in events:
        try:
            data = json.loads(e["data_json"] or "{}")
        except Exception:  # noqa: BLE001
            data = {}
        parent = spans.get(e["parent_id"], root)
        start = _ns(e["ts"])
        end = start + int(e["duration_ms"] or 0) * 1_000_000
        span = tracer.start_span(
            f"{e['kind']}/{e['name']}",
            context=set_span_in_context(parent),
            start_time=start,
            attributes=_attributes(e["kind"], e["name"] or "", run_id, data),
        )
        status = e["status"] or ""
        if status in _ERROR_STATUSES or e["error"]:
            span.set_status(Status(StatusCode.ERROR, (e["error"] or status)[:500]))
        spans[e["event_id"]] = span
        order.append(e["event_id"])
        ends[e["event_id"]] = end
        last_end = max(last_end, end)

    # End children before parents (reverse insertion order).
    for eid in reversed(order):
        spans[eid].end(end_time=ends[eid])
    root.end(end_time=run_end or last_end)

    if not provider.force_flush(timeout_millis=15000):
        raise RuntimeError(f"OTLP export to {endpoint} was not acknowledged")
    provider.shutdown()
    return len(order) + 1


def list_run_ids(db_path: str | Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute("SELECT run_id FROM runs ORDER BY started_at")]
    finally:
        conn.close()
