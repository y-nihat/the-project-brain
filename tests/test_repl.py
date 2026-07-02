"""REPL event formatting + the tracer live-listener hook (all pure)."""
from __future__ import annotations

from brain.observability import Tracer
from brain.repl import format_event


def test_format_event_renders_key_moments():
    assert "2 agent(s)" in format_event(
        {"kind": "plan.created", "name": "plan",
         "data": {"agents": [{"id": "a"}, {"id": "b"}]}}
    )
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
    assert format_event({"kind": "agent.tool_call", "name": "mcp__caps__web_search"}) is None
    assert format_event({"kind": "decision", "name": "x", "data": {"choice": "noop"}}) is None


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
