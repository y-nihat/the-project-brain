"""REPL event formatting + the tracer live-listener hook (all pure)."""
from __future__ import annotations

from brain.observability import Tracer
from brain.repl import format_event


def test_format_event_renders_key_moments():
    plan_line = format_event(
        {"kind": "plan.created", "name": "plan",
         "data": {"stages": [{"id": "gather", "pattern": "single"},
                             {"id": "survey", "pattern": "map"}]}}
    )
    assert "2 stage(s)" in plan_line and "survey(map)" in plan_line
    # Legacy records (pre-workflow runs) still render.
    assert "2 stage(s)" in format_event(
        {"kind": "plan.created", "name": "plan",
         "data": {"agents": [{"id": "a"}, {"id": "b"}]}}
    )
    assert "fan-out" in format_event(
        {"kind": "map.fanout", "name": "survey",
         "data": {"items": 12, "workers": 3, "shard_size": 5}}
    )
    assert "review round 1: revise" in format_event(
        {"kind": "review.verdict", "name": "check", "data": {"round": 1, "verdict": "revise"}}
    )
    assert "ok (5 results)" in format_event(
        {"kind": "tool.result", "name": "web_search", "data": {"result": "ok", "count": 5}}
    )
    assert "degraded" in format_event(
        {"kind": "tool.result", "name": "web_search", "data": {"result": "empty", "count": 0}}
    )
    assert "stage survey [map]" in format_event(
        {"kind": "stage", "name": "survey", "phase": "start", "data": {"pattern": "map"}}
    )
    # A completed stage streams the head of its output — mid-run text meant for
    # the user (a how-to, a warning) must not hide in a file until the end.
    done = format_event(
        {"kind": "stage", "name": "prepare", "phase": "end", "duration_ms": 1000,
         "data": {"pattern": "single",
                  "output_head": "Before continuing you will need:\n1. an account\n2. an API key"}}
    )
    assert "stage prepare done" in done
    assert "an API key" in done and "│" in done
    assert "outputs/prepare.md" in done
    assert "agent ▸ Researcher" in format_event(
        {"kind": "agent.created", "name": "r", "data": {"role": "Researcher", "capabilities": ["web_search"]}}
    )
    assert "authoring" in format_event(
        {"kind": "decision", "name": "x", "data": {"choice": "author", "capability": "gen_pdf"}}
    )
    assert "reuse tool" in format_event(
        {"kind": "decision", "name": "x", "data": {"choice": "reuse", "capability": "web_search"}}
    )
    v_ok = format_event({"kind": "capability.verify", "name": "t", "phase": "end",
                         "duration_ms": 2000, "data": {"passed": True}})
    assert "✓" in v_ok
    v_bad = format_event({"kind": "capability.verify", "name": "t", "phase": "end",
                          "duration_ms": 500, "data": {"passed": False, "detail": "boom"}})
    assert "✗" in v_bad and "boom" in v_bad
    assert "approval needed" in format_event(
        {"kind": "approval.requested", "name": "x", "data": {"action": "use BotFather token"}}
    )


def test_format_event_skips_noise():
    assert format_event({"kind": "run", "name": "start"}) is None
    assert format_event({"kind": "decision", "name": "x", "data": {"choice": "noop"}}) is None


def test_format_event_shows_tool_call_args():
    line = format_event({"kind": "agent.tool_call", "name": "mcp__caps__web_search",
                         "data": {"args_preview": "query=ISO 42001 adoption"}})
    assert "web_search" in line and "ISO 42001 adoption" in line


def test_tracer_listener_receives_events_and_survives_errors(tmp_path):
    seen = []
    tracer = Tracer(tmp_path / "b.db", tmp_path / "runs")
    tracer.add_listener(lambda rec: seen.append(rec))
    tracer.add_listener(lambda rec: (_ for _ in ()).throw(RuntimeError("boom")))  # must not break

    run = tracer.start_run("do a thing")
    tracer.event("plan.created", "plan", status="info", agents=[{"id": "a"}])
    with tracer.span("capability.build", "gen", attempt=1):
        pass
    run.finish("succeeded", outcome="done")
    tracer.close()

    kinds = [r.get("kind") for r in seen]
    assert "plan.created" in kinds
    assert "capability.build" in kinds
    # both a start (running) and an end (with duration) were delivered for the span
    builds = [r for r in seen if r.get("kind") == "capability.build"]
    assert any(r.get("phase") == "start" for r in builds)
    assert any(r.get("duration_ms") is not None for r in builds)
