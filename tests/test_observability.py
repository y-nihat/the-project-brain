"""Tracer unit test — no Docker/SDK/token needed; runs anywhere.

Drives a Tracer through a simulated run (nested spans + point events + an
approval + a sandbox.run) and asserts the persisted trace, rollups, nesting, and
the Markdown export + viewer rendering.
"""
from __future__ import annotations

import json
import sqlite3

from brain.observability import Tracer
from brain.observability import view


def test_tracer_records_run_with_nesting_rollups_and_export(tmp_path):
    db = tmp_path / "t.db"
    runs = tmp_path / "runs"
    tracer = Tracer(db, runs, engine_version="9.9")

    run = tracer.start_run("do a thing", model="m")
    rid = run.run_id

    tracer.event("plan.created", "plan", status="info",
                 tasks=[{"id": "t1", "capability_need": "web_search"}])
    with tracer.span("task.resolve", "t1", capability_need="web_search"):
        tracer.event("decision", "t1", status="info", choice="create", capability="web_search")
        with tracer.span("capability.acquire", "web_search"):
            with tracer.span("capability.build", "web_search", image="img"):
                pass
            with tracer.span("capability.verify", "web_search") as v:
                v.set(passed=True, detail="3 results")
        with tracer.span("sandbox.run", "web_search", image="img", network="bridge") as sp:
            sp.set(exit_code=0, timed_out=False, ledger_id="cid123")
    tracer.event("approval.requested", "x", status="info", action="do x")
    tracer.event("approval.resolved", "x", status="denied", outcome="denied")

    run.finish("succeeded", outcome="the final answer")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT * FROM runs WHERE run_id=?", (rid,)).fetchone()
        assert r["status"] == "succeeded"
        assert r["engine_version"] == "9.9"
        assert r["n_containers"] == 1        # one sandbox.run
        assert r["n_approvals"] == 1         # one approval.requested
        assert r["duration_ms"] is not None

        events = conn.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (rid,)).fetchall()
        # seq is dense and chronological
        assert [e["seq"] for e in events] == list(range(len(events)))
        assert r["n_events"] == len(events)

        by_kind: dict[str, list] = {}
        for e in events:
            by_kind.setdefault(e["kind"], []).append(e)

        # nesting: build's parent is the acquire span; decision's parent is task.resolve
        acquire_id = by_kind["capability.acquire"][0]["event_id"]
        assert by_kind["capability.build"][0]["parent_id"] == acquire_id
        resolve_id = by_kind["task.resolve"][0]["event_id"]
        assert by_kind["decision"][0]["parent_id"] == resolve_id
        # approval events are at the root (emitted outside any span)
        assert by_kind["approval.requested"][0]["parent_id"] is None

        # verify span captured its result + a duration
        verify = by_kind["capability.verify"][0]
        assert verify["duration_ms"] is not None
        assert json.loads(verify["data_json"]).get("passed") is True
    finally:
        conn.close()

    # Markdown timeline exported and readable
    md = runs / rid / "trace.md"
    assert md.exists()
    text = md.read_text()
    for token in ("# Run", "## Timeline", "the final answer", "web_search"):
        assert token in text

    # viewer renders without error
    assert rid in view.render_list(db)
    rendered = view.render_run(db, rid)
    assert "capability.verify" in rendered and "sandbox.run" in rendered

    tracer.close()


def test_artifact_path_creates_run_dirs(tmp_path):
    from brain.observability import NullTracer, Tracer

    tracer = Tracer(tmp_path / "t.db", tmp_path / "runs")
    run = tracer.start_run("obj")
    p = tracer.artifact_path("web_search", "promoted")
    assert p.is_dir()
    assert p == tmp_path / "runs" / run.run_id / "artifacts" / "web_search" / "promoted"
    (p / "search.py").write_text("print('x')")
    assert (p / "search.py").read_text() == "print('x')"
    # NullTracer returns a usable throwaway dir (no-op safe for untraced callers)
    assert NullTracer().artifact_path("x").is_dir()
    tracer.close()


def test_wait_for_collector_returns_once_reachable(monkeypatch):
    """Boot-wait: retries quietly while the viewer starts, returns once any
    HTTP response arrives, and gives up after the deadline without raising."""
    import urllib.request

    from brain.observability import otel_export

    monkeypatch.setattr(otel_export.time, "sleep", lambda _s: None)
    attempts = {"n": 0}

    def flaky(url, timeout=0):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionRefusedError("not up yet")
        return object()

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    otel_export.wait_for_collector("http://phoenix:6006/v1/traces", timeout_s=30)
    assert attempts["n"] == 3

    def never_up(url, timeout=0):
        raise ConnectionRefusedError("still down")

    monkeypatch.setattr(urllib.request, "urlopen", never_up)
    otel_export.wait_for_collector("http://phoenix:6006/v1/traces", timeout_s=0.05)  # no raise
