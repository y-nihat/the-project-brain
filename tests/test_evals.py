"""Eval harness mechanics — all pure (facts from a fabricated trace, checks,
scoring, scorecard rendering). The live suite itself costs money and is run
via scripts/run-evals.sh, not pytest.
"""
from __future__ import annotations

from brain.evals import checks, load_facts, score_run
from brain.evals.runner import render_markdown
from brain.evals.scenarios import SCENARIOS, get, validate
from brain.observability import Tracer


def _fabricate_run(tmp_path, *, answer: str, stages=None, events=(), status="succeeded"):
    """Record a synthetic but structurally real run through the actual Tracer."""
    db, runs_dir = tmp_path / "b.db", tmp_path / "runs"
    tracer = Tracer(db, runs_dir)
    run = tracer.start_run("survey sources and report")
    tracer.event("plan.created", "plan", status="info",
                 stages=stages or [{"id": "gather", "pattern": "single"},
                                   {"id": "survey", "pattern": "map"},
                                   {"id": "merge", "pattern": "reduce"}])
    for kind, name, data in events:
        tracer.event(kind, name, status="info", **data)
    with tracer.span("agent.session", "gather"):
        pass
    run.finish(status, outcome=answer)
    rid = run.run_id
    tracer.close()
    out = runs_dir / rid / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    (out / "ANSWER.md").write_text(answer)
    return db, runs_dir, rid


def test_load_facts_reads_trace_and_data_layer(tmp_path):
    db, runs_dir, rid = _fabricate_run(
        tmp_path,
        answer="Brief with sources: https://a.example/x and https://b.example/y.",
        events=[
            ("map.fanout", "survey", {"items": 12, "workers": 3, "shard_size": 5}),
            ("tool.result", "web_search", {"result": "ok", "count": 5}),
            ("tool.result", "web_search", {"result": "empty", "count": 0}),
            ("review.verdict", "check", {"round": 1, "verdict": "approved"}),
            ("decision", "web_search", {"choice": "reuse", "capability": "web_search"}),
            ("team.question", "survey.w01", {"to": "gather", "question": "?"}),
            ("team.blocker", "survey.w02", {"issue": "404"}),
        ],
    )
    f = load_facts(db, runs_dir, rid)
    assert f.status == "succeeded"
    assert f.patterns_used == {"single", "map", "reduce"}
    assert f.fanout_workers == 3
    assert f.tool_results == {"ok": 1, "empty": 1}
    assert f.review_verdicts == ["approved"]
    assert f.capabilities_reused == ["web_search"]
    assert f.sessions == 1
    assert len(f.cited_urls) == 2
    assert f.team_questions == 1 and f.team_blockers == 1
    ok, detail = checks.team_activity(f, min_count=2)
    assert ok and "2" in detail


def test_checks_pass_and_fail(tmp_path):
    db, runs_dir, rid = _fabricate_run(
        tmp_path, answer="short answer, no links",
        stages=[{"id": "solo", "pattern": "single"}],
    )
    f = load_facts(db, runs_dir, rid)
    assert checks.status_is(f)[0]
    assert checks.max_stages(f, limit=1)[0]
    assert checks.no_tools_used(f)[0]
    assert not checks.cites_url(f, min_count=1)[0]
    assert not checks.used_pattern(f, pattern="map")[0]
    ok, detail = checks.answer_max_chars(f, limit=5)
    assert not ok and "length" in detail


def test_score_run_and_scorecard_render(tmp_path):
    db, runs_dir, rid = _fabricate_run(
        tmp_path, answer="one concise sentence.",
        stages=[{"id": "solo", "pattern": "single"}],
    )
    card = score_run(rid, get("tiny-no-tools"), db_path=db, runs_dir=runs_dir)
    assert card.ok, card.results
    md = render_markdown([card])
    assert "tiny-no-tools" in md and "✅" in md and "| status_is | ✓" in md

    # A failing card renders visibly too.
    card2 = score_run(rid, get("multi-source-brief"), db_path=db, runs_dir=runs_dir)
    assert not card2.ok
    assert "❌" in render_markdown([card2])


def test_scenarios_validate_against_check_registry():
    validate()
    assert len(SCENARIOS) >= 4
    # Every referenced check binds cleanly to its params (signature sanity).
    for s in SCENARIOS:
        for name, params in s.checks:
            assert name in checks.CHECKS
            assert isinstance(params, dict)
