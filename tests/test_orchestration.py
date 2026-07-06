"""Staged workflows end-to-end with a stub session runner — no Docker, no LLM.

The stub runner plays every agent: the gatherer emits a 12-item manifest, map
workers echo their shard, the reducer drafts, and the reviewer rejects once
then approves — exercising runtime fan-out, consolidation, and the
review/revise loop exactly as the orchestrator drives them.
"""
from __future__ import annotations

import asyncio
import json

from brain import config
from brain.memory.blackboard import Blackboard
from brain.models import StageSpec
from brain.orchestrator import Orchestrator
from brain.planner import StubPlanner

ITEMS = [f"https://site{i}.example/post" for i in range(12)]


def _stages() -> list[StageSpec]:
    return [
        StageSpec(id="gather", pattern="single", role="Scout",
                  goal="enumerate the sources to study"),
        StageSpec(id="survey", pattern="map", role="Analyst",
                  goal="study your assigned sources in depth",
                  depends_on=["gather"], shard_size=5),
        StageSpec(id="synthesize", pattern="reduce", role="Editor",
                  goal="merge the workers' notes into one report",
                  depends_on=["survey"]),
        StageSpec(id="check", pattern="review", role="Critic",
                  goal="verify the report against the notes",
                  depends_on=["synthesize"], rounds=2),
    ]


def _orchestrator(tmp_path, runner, stages=None) -> tuple[Orchestrator, Blackboard]:
    bb = Blackboard(tmp_path / "b.db", "wf-test")
    orch = Orchestrator(
        session="wf-test", manager=None, ledger=None, registry=None,
        blackboard=bb, broker=None, planner=StubPlanner(stages or _stages()),
        resolver=None, use_builder=False, session_runner=runner,
    )
    return orch, bb


def test_workflow_fanout_reduce_review(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    calls: list[tuple[str, str]] = []
    review_verdicts = iter(["VERDICT: revise\n1. drop the unsupported claim.",
                            "VERDICT: approved\nAll claims are supported."])

    async def runner(tracer, name, prompt, options):
        calls.append((name, prompt))
        if name == "gather":
            return "Found things.\n```manifest\n" + json.dumps(ITEMS) + "\n```"
        if name.startswith("survey.w"):
            return f"notes({name})"
        if name == "synthesize":
            return "DRAFT v1"
        if name == "synthesize.fix":
            return "DRAFT v2"
        if name.startswith("check.r"):
            return next(review_verdicts)
        raise AssertionError(f"unexpected session: {name}")

    orch, bb = _orchestrator(tmp_path, runner)
    try:
        answer = asyncio.run(orch.run("study the sources and report"))
    finally:
        bb.close()

    names = [n for n, _ in calls]
    # Runtime fan-out: 12 items / shard_size 5 -> exactly 3 workers.
    assert [n for n in names if n.startswith("survey.w")] == [
        "survey.w01", "survey.w02", "survey.w03"]
    # The gatherer was told how to emit a manifest (engine mechanics, not planner prose).
    gather_prompt = dict(calls)["gather"]
    assert "```manifest" in gather_prompt
    # Each worker saw only its own shard.
    w1_prompt = dict(calls)["survey.w01"]
    assert ITEMS[0] in w1_prompt and ITEMS[5] not in w1_prompt
    # The reducer received every worker's notes.
    reduce_prompt = dict(calls)["synthesize"]
    assert all(f"notes(survey.w{k:02d})" in reduce_prompt for k in (1, 2, 3))
    # Review round 1 rejected -> the reducer was re-run with the critique -> approved.
    assert "synthesize.fix" in names and "check.r2" in names
    fix_prompt = dict(calls)["synthesize.fix"]
    assert "drop the unsupported claim" in fix_prompt
    # The final answer is the REVISED draft, and it landed in ANSWER.md.
    assert answer == "DRAFT v2"
    assert (tmp_path / "runs" / "wf-test" / "outputs" / "ANSWER.md").read_text() == "DRAFT v2"
    # Worker notes are first-class data-layer files.
    assert (tmp_path / "runs" / "wf-test" / "outputs" / "survey.w02.md").exists()


def test_map_without_manifest_degrades_to_one_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    calls: list[str] = []

    async def runner(tracer, name, prompt, options):
        calls.append(name)
        if name == "gather":
            return "prose with no manifest at all"
        return f"out({name})"

    stages = _stages()[:3]  # gather -> map -> reduce, no review
    orch, bb = _orchestrator(tmp_path, runner, stages)
    try:
        answer = asyncio.run(orch.run("study"))
    finally:
        bb.close()
    # Degraded honestly: exactly one worker over the raw upstream output.
    assert [n for n in calls if n.startswith("survey.w")] == ["survey.w01"]
    assert answer == "out(synthesize)"


def test_session_cap_stops_new_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "RUN_MAX_AGENT_SESSIONS", 2)

    async def runner(tracer, name, prompt, options):
        if name == "gather":
            return "```manifest\n" + json.dumps(ITEMS) + "\n```"
        return f"out({name})"

    orch, bb = _orchestrator(tmp_path, runner)
    try:
        answer = asyncio.run(orch.run("study"))
    finally:
        bb.close()
    # gather + first worker used the budget; everything after is honestly not-run.
    assert "cap" in answer or "not run" in answer


def test_reduce_batches_hierarchically(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "REDUCE_MAX_INPUT_CHARS", 200)  # force batching
    calls: list[str] = []

    async def runner(tracer, name, prompt, options):
        calls.append(name)
        if name == "gather":
            return "```manifest\n" + json.dumps(ITEMS) + "\n```"
        if name.startswith("survey.w"):
            return "N" * 150  # each worker's notes ~150 chars -> 3 notes never fit 200
        if name.startswith("synthesize.b"):
            return f"partial({name})"
        return "FINAL"

    stages = _stages()[:3]
    orch, bb = _orchestrator(tmp_path, runner, stages)
    try:
        answer = asyncio.run(orch.run("study"))
    finally:
        bb.close()
    partials = [n for n in calls if n.startswith("synthesize.b")]
    assert len(partials) >= 2                # hierarchical: partial merges ran
    assert calls[-1] == "synthesize" and answer == "FINAL"
