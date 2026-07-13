"""Brain-decided persistence (Component 2): at a failure/recovery boundary the
SUPERVISOR judges — from the attempt history and the expectation gap — whether
to keep trying, stop and ask the user, or stop, instead of a flat counter. It
can only make the run stop EARLIER; a hard ceiling remains the safety rail. When
it stops for input, the run emits a structured help request (how far / what
tried / why / what it needs). Stubbed — live behavior is validated in runs."""
from __future__ import annotations

import asyncio

from brain import config, workflow
from brain.acquisition import AcquisitionError
from brain.memory.blackboard import Blackboard
from brain.models import CREATE, Decision, StageSpec
from brain.observability import NullTracer
from brain.orchestrator import Orchestrator
from brain.planner import StubPlanner
from brain.supervisor import Supervisor


class CreateResolver:
    def resolve(self, need):
        return Decision(kind=CREATE, capability_need=need)


class RecoveringPlanner(StubPlanner):
    def __init__(self, stages, extension):
        super().__init__(stages)
        self._ext = list(extension)
        self.extend_calls = 0

    async def extend(self, objective, completed, gaps, existing_ids):
        self.extend_calls += 1
        return ([s for s in self._ext if s.id not in set(existing_ids)], [], {})


def _fail_acquire():
    async def stub(need, **kwargs):
        raise AcquisitionError(f"cannot build '{need}'", need=need,
                               failure_class="implementation", attempts=3,
                               history=["attempt 1: implementation — API mismatch",
                                        "attempt 2: implementation — still failing"])
    return stub


def _supervisor(persist_action, needed="", reason="r"):
    async def decide(moment, digest):
        if moment == "persistence":
            return {"action": persist_action, "needed": needed, "reason": reason}
        return {"action": "proceed"}
    return Supervisor(decide_fn=decide, tracer=NullTracer())


def _run(tmp_path, monkeypatch, session, planner, supervisor):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr("brain.orchestrator.acquire", _fail_acquire())
    bb = Blackboard(tmp_path / "b.db", session)

    async def runner(_t, name, prompt, options):
        if name == "observe_alt":
            # Simulate a REAL retention: payload + provenance, as the runtime
            # records it (stubs must honor the contract the gate checks).
            from brain import evidence
            d = tmp_path / "runs" / session / "workspace" / "obs"
            d.mkdir(parents=True, exist_ok=True)
            (d / "payload.txt").write_text("observed")
            evidence.record(tmp_path / "runs" / session, "obs", ["obs/payload.txt"],
                            ["https://example.test/v/abc"], "complete")
            return "observed via the alternative method"
        if name.startswith("review"):
            import json as _json
            return ("VERDICT: approved\n```claims\n"
                    + _json.dumps([{"claim": "the load-bearing claim",
                                    "support": "direct",
                                    "evidence": "obs/payload.txt"}]) + "\n```")
        return f"out({name})"

    orch = Orchestrator(session=session, manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner,
                        resolver=CreateResolver(), session_runner=runner,
                        supervisor=supervisor, tracer=NullTracer())
    try:
        answer = asyncio.run(orch.run("Explain https://example.test/v/abc"))
    finally:
        bb.close()
    return orch, answer


def _plan():
    return RecoveringPlanner(
        [StageSpec(id="observe", pattern="single", role="O",
                   goal="observe the designated source", capabilities=["fetch_it"])],
        [StageSpec(id="observe_alt", pattern="single", role="A",
                   goal="observe it a different way")])


# --- the three judgments -------------------------------------------------------- #
def test_keep_trying_continues_recovery(tmp_path, monkeypatch):
    planner = _plan()
    orch, answer = _run(tmp_path, monkeypatch, "keep", planner, _supervisor("keep_trying"))
    assert planner.extend_calls == 1                 # recovery proceeded
    assert "alternative method" in answer
    assert orch.classification == "succeeded"
    assert orch._help_request is None


def test_stop_and_ask_emits_a_structured_help_request(tmp_path, monkeypatch):
    planner = _plan()
    orch, answer = _run(tmp_path, monkeypatch, "ask", planner,
                        _supervisor("stop_and_ask",
                                    needed="please provide an accessible copy of the source file",
                                    reason="the source cannot be retrieved by any available method"))
    assert planner.extend_calls == 0                 # did NOT keep trying
    assert orch.classification == "blocked"
    assert orch._help_request is not None
    assert "I need your input to continue" in answer
    assert "please provide an accessible copy" in answer
    assert "What I tried:" in answer and "Why I stopped:" in answer
    assert "stopped to ask for your input" in answer.split("\n")[0] or \
           "stopped to ask for your input" in answer


def test_stop_is_a_calibrated_stop(tmp_path, monkeypatch):
    planner = _plan()
    orch, answer = _run(tmp_path, monkeypatch, "stop", planner,
                        _supervisor("stop", reason="no admissible path exists"))
    assert planner.extend_calls == 0
    assert orch._help_request is None
    assert orch.classification == "failed"           # nothing completed, no help needed
    assert "no admissible path" in orch._recovery_state


def test_fail_open_keeps_trying_when_supervisor_errors(tmp_path, monkeypatch):
    async def boom(moment, digest):
        raise RuntimeError("supervisor down")
    planner = _plan()
    orch, answer = _run(tmp_path, monkeypatch, "failopen", planner,
                        Supervisor(decide_fn=boom, tracer=NullTracer()))
    assert planner.extend_calls == 1                 # errored supervision -> keep trying
    assert orch.classification == "succeeded"


def test_hard_ceiling_still_bounds_even_if_supervisor_says_keep_trying(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RECOVERIES_PER_RUN", 0)   # ceiling reached immediately
    planner = _plan()
    orch, _ = _run(tmp_path, monkeypatch, "ceiling", planner, _supervisor("keep_trying"))
    assert planner.extend_calls == 0                 # ceiling wins over the supervisor
    assert "budget exhausted" in orch._recovery_state


# --- supervisor unit: coercion + fail-open ------------------------------------- #
def test_supervisor_persistence_coerces_and_fails_open():
    async def good(m, d):
        return {"action": "stop_and_ask", "needed": "a file", "reason": "stuck"}
    dec = asyncio.run(Supervisor(decide_fn=good).persistence("digest"))
    assert dec.action == "stop_and_ask" and dec.message == "a file"

    async def garbage(m, d):
        return {"action": "banana"}
    assert asyncio.run(Supervisor(decide_fn=garbage).persistence("d")).action == "keep_trying"

    async def crash(m, d):
        raise ValueError("x")
    assert asyncio.run(Supervisor(decide_fn=crash).persistence("d")).action == "keep_trying"


def test_supervisor_disabled_keeps_trying(monkeypatch):
    monkeypatch.setattr(config, "SUPERVISOR_ENABLED", False)
    assert asyncio.run(Supervisor().persistence("d")).action == "keep_trying"


# --- the expectation field flows through ---------------------------------------- #
def test_expectation_is_parsed_and_reaches_the_digest(tmp_path):
    stages, _ = workflow.normalize_stages([
        {"id": "obs", "pattern": "single", "role": "O", "goal": "observe it",
         "expectation": "full-duration coverage of the visual content"}])
    assert stages[0].expectation == "full-duration coverage of the visual content"

    bb = Blackboard(tmp_path / "b.db", "exp")
    orch = Orchestrator(session="exp", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=None, resolver=None,
                        tracer=NullTracer())
    orch._run_ctx = ("the objective", None, tmp_path / "ws")
    from brain.models import MethodOutcome
    outcome = MethodOutcome(stage_id="obs", requirement="observe it", phase="acquisition",
                            failure_class="implementation", detail="failed", attempts=2)
    orch._failures.append(outcome)
    digest = orch._persistence_digest(stages[0], outcome)
    bb.close()
    assert "EXPECTED YIELD" in digest
    assert "full-duration coverage" in digest
    assert "keep_trying" in digest and "stop_and_ask" in digest
