"""Discovery-audit remediation (v0.28.0): defects found by reading the engine
against its own 107-run corpus rather than by a failing run.

Each test below pins an invariant the audit found stated but not enforced.
"""
from __future__ import annotations

import asyncio

from brain import config, evidence
from brain.memory.blackboard import Blackboard
from brain.models import StageSpec
from brain.observability import NullTracer
from brain.orchestrator import Orchestrator
from brain.planner import StubPlanner


class RecordingTracer(NullTracer):
    """Minimal recorder: the kinds of events the run emitted."""

    def __init__(self):
        self._kinds: list[str] = []

    def event(self, kind, name, status="info", **data):
        self._kinds.append(kind)

    def kinds(self):
        return list(self._kinds)


def _orch(tmp_path, runner, planner, session, tracer=None):
    bb = Blackboard(tmp_path / f"{session}.db", session)
    orch = Orchestrator(session=session, manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner, tracer=tracer)
    return orch, bb


def _prior_run(tmp_path, run_id="unrelated-run"):
    prior = tmp_path / "runs" / run_id
    (prior / "outputs").mkdir(parents=True)
    (prior / "workspace").mkdir()
    # ANSWER.md is the agent's NARRATION envelope — never evidence.
    (prior / "outputs" / "ANSWER.md").write_text("I concluded the widget is blue.\n")
    return prior


# --- an open standing goal is not a session continuation -------------------------- #
def test_open_goal_context_does_not_seed_an_unrelated_run_as_evidence(
        tmp_path, monkeypatch):
    """The audit's sharpest finding. `session_context` carries BOTH the
    previous turn of this session AND the open-standing-goals block. Deriving
    "this run continues a session" from that block being non-empty meant one
    open goal made every later run — in any session — seed whatever run
    keyword-matching picked, and record its ANSWER.md (narration) as a
    DESIGNATED source, i.e. target-tier evidence about a target it never
    observed. `agent prose -> NEVER in the ledger` (ARCHITECTURE.md §6)."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    _prior_run(tmp_path)
    tracer = RecordingTracer()

    async def runner(_t, name, prompt, options):
        return "done"

    planner = StubPlanner([StageSpec(id="work", pattern="single", role="E",
                                     goal="produce the thing")])
    orch, bb = _orch(tmp_path, runner, planner, "goalrun", tracer)
    try:
        asyncio.run(orch.run(
            "measure the throughput and report it",
            seed_run="unrelated-run",
            # Exactly what session.py builds when a goal is open on a FRESH
            # session: goals block present, no previous turn.
            session_context="STANDING GOALS (open — ...):\n- goal #1: throughput",
            continues_session=False))
    finally:
        bb.close()
    ws = tmp_path / "runs" / "goalrun" / "workspace"
    assert not (ws / "prior" / "unrelated-run" / "outputs" / "ANSWER.md").exists()
    assert "revision.deliverable_seeded" not in tracer.kinds()
    # And nothing was designated: the ledger holds no laundered narration.
    ledger = evidence.ledger(tmp_path / "runs" / "goalrun", [], [])
    assert not [e for e in ledger if "unrelated-run" in str(e.file)]


def test_a_real_session_turn_still_seeds_the_prior_deliverable(tmp_path, monkeypatch):
    """The complement: continuity stated as a FACT still seeds, so the fix
    removes the false trigger without weakening the real one."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    prior = _prior_run(tmp_path, "prev-run")
    (prior / "outputs" / "DELIVERABLE.md").write_text("### Q: one\nanswer\n")
    tracer = RecordingTracer()

    async def runner(_t, name, prompt, options):
        return "revised"

    planner = StubPlanner([StageSpec(id="revise", pattern="single", role="E",
                                     goal="update the affected answers")])
    orch, bb = _orch(tmp_path, runner, planner, "sessrun", tracer)
    try:
        asyncio.run(orch.run("the details changed; update it",
                             seed_run="prev-run",
                             session_context="previous run id: prev-run",
                             continues_session=True))
    finally:
        bb.close()
    ws = tmp_path / "runs" / "sessrun" / "workspace"
    assert (ws / "prior" / "prev-run" / "outputs" / "DELIVERABLE.md").is_file()
    assert "revision.deliverable_seeded" in tracer.kinds()
