"""Executive supervision: bounded decisions, fail-open safety, and the two
consult moments (pre-interaction, stage-completed) wired through the
orchestrator — all pure (stub decide_fn + stub session runner)."""
from __future__ import annotations

import asyncio
import json

from brain import config
from brain.approval import ApprovalBroker
from brain.memory.blackboard import Blackboard
from brain.models import REUSE, Capability, Decision as ResolveDecision, StageSpec
from brain.orchestrator import Orchestrator
from brain.planner import StubPlanner
from brain.supervisor import Decision, Supervisor, coerce_decision


# --- decision coercion --------------------------------------------------------- #
def test_coerce_decision_bounds_the_model():
    assert coerce_decision({"action": "abort", "reason": "r"}).action == "abort"
    # Unknown action, junk types, missing payloads all degrade to proceed.
    assert coerce_decision({"action": "reboot_the_universe"}).action == "proceed"
    assert coerce_decision("not a dict").action == "proceed"
    assert coerce_decision({"action": "inform_user", "message": "  "}).action == "proceed"
    assert coerce_decision({"action": "redo_stage", "guidance": ""}).action == "proceed"
    # Disallowed-for-this-moment actions degrade too.
    assert coerce_decision({"action": "redo_stage", "guidance": "g"},
                           allowed=("proceed", "inform_user")).action == "proceed"


def test_supervisor_fail_open_and_budget():
    async def explode(moment, digest):
        raise RuntimeError("model unavailable")

    s = Supervisor(decide_fn=explode, max_calls=5)
    d = asyncio.run(s.consult("pre_interaction", "digest"))
    assert d.action == "proceed" and "fail-open" in d.reason

    async def fine(moment, digest):
        return {"action": "abort", "reason": "r"}

    s2 = Supervisor(decide_fn=fine, max_calls=1)
    assert asyncio.run(s2.consult("m", "d")).action == "abort"
    over = asyncio.run(s2.consult("m", "d"))            # budget exhausted
    assert over.action == "proceed" and "budget" in over.reason


# --- orchestrator wiring -------------------------------------------------------- #
class _StubResolver:
    def __init__(self, cap):
        self.cap = cap

    def resolve(self, need):
        return ResolveDecision(kind=REUSE, capability_need=need, capability=self.cap)


def test_pre_interaction_informs_user_before_the_prompt(tmp_path, monkeypatch):
    """The supervisor sees the pending credential ask, notices the user asked to
    be told how to obtain it, and the instructions print BEFORE the prompt."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    timeline: list[str] = []
    digests: list[str] = []

    async def decide(moment, digest):
        digests.append(f"{moment}:{digest}")
        if moment == "pre_interaction":
            return {"action": "inform_user", "reason": "user asked for retrieval steps",
                    "message": "To obtain the key: 1. open the provider console 2. copy it"}
        return {"action": "proceed", "reason": "fine"}

    cap = Capability(name="svc_call", description="calls the service", image_tag="i:1",
                     command=["x"], input_schema={}, env_map={},
                     requires_credential="SVC_KEY", verified=True)
    broker = ApprovalBroker(
        input_fn=lambda p: (timeline.append(f"ASK:{p}"), "y")[1],
        secret_input_fn=lambda p: (timeline.append(f"SECRET:{p}"), "key-1")[1],
        output_fn=lambda s: timeline.append(f"OUT:{s}"))

    async def runner(tracer, name, prompt, options):
        if name == "use_service":
            return "done"
        raise AssertionError(name)

    bb = Blackboard(tmp_path / "b.db", "sup-test")
    orch = Orchestrator(
        session="sup-test", manager=None, ledger=None, registry=None,
        blackboard=bb, broker=broker,
        planner=StubPlanner([StageSpec(id="use_service", pattern="single", role="Caller",
                                       goal="call the service", capabilities=["svc_call"])]),
        resolver=_StubResolver(cap), session_runner=runner,
        supervisor=Supervisor(decide_fn=decide))
    try:
        asyncio.run(orch.run("Call the service; ask me for the key and tell me how to get it"))
    finally:
        bb.close()

    informed = next(i for i, s in enumerate(timeline) if "provider console" in s)
    prompted = next(i for i, s in enumerate(timeline) if s.startswith("SECRET:"))
    assert informed < prompted
    # The supervisor saw the objective and the pending ask in its digest.
    assert "pre_interaction" in digests[0]
    assert "tell me how to get it" in digests[0] and "SVC_KEY" in digests[0]


def test_stage_completed_redo_sends_the_stage_back(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    calls: list[tuple[str, str]] = []
    consults: list[str] = []

    async def decide(moment, digest):
        consults.append(moment)
        # Send the writer back exactly once, with concrete guidance.
        if moment == "stage_completed" and "JUST COMPLETED — stage 'write'" in digest \
                and "only two items" in digest:
            return {"action": "redo_stage", "reason": "objective demands three",
                    "guidance": "produce THREE items as the objective requires"}
        return {"action": "proceed", "reason": "ok"}

    async def runner(tracer, name, prompt, options):
        calls.append((name, prompt))
        if name == "research":
            return "found things"
        if name == "write":
            return "only two items"
        if name == "write.fix":
            return "now three items"
        raise AssertionError(name)

    stages = [
        StageSpec(id="research", pattern="single", role="R", goal="research"),
        StageSpec(id="write", pattern="single", role="W", goal="write three items",
                  depends_on=["research"]),
    ]
    bb = Blackboard(tmp_path / "b2.db", "sup-redo")
    orch = Orchestrator(session="sup-redo", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None,
                        planner=StubPlanner(stages), resolver=None,
                        session_runner=runner, supervisor=Supervisor(decide_fn=decide))
    try:
        answer = asyncio.run(orch.run("write three items"))
    finally:
        bb.close()

    assert answer == "now three items"
    fix_prompt = dict(calls)["write.fix"]
    assert "THREE items" in fix_prompt
    assert "only two items" in fix_prompt          # redo saw its prior output
    assert consults.count("stage_completed") == 2  # once per stage, no loop on the redo
