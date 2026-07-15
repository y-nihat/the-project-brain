"""v0.20 — claim strength must match evidence strength (post-cycle-3 owner
review of campaign c20260715-0608): the campaign's first REAL false success
was not a fabrication but an INCOMPLETE SWEEP CERTIFIED COMPLETE — a
'every place where documents disagree' criterion was verified existentially
(each listed discrepancy was genuine) while the pack's primary planted
contradiction, derivable by one multiplication between two sources, went
unlisted and unmissed; and a synthesis cited a timestamp to the millisecond
that no retained observation carried — precision theater the reviewer
rightly refused, three rounds too late to fix without re-observation."""
from __future__ import annotations

import asyncio

from brain import config, factory
from brain.memory.blackboard import Blackboard
from brain.models import AgentSpec, StageSpec
from brain.observability import NullTracer
from brain.orchestrator import Orchestrator
from brain.planner import StubPlanner


def test_agent_contract_binds_claim_strength_to_evidence():
    text = factory.agent_system_prompt(
        AgentSpec(id="a", role="R", goal="g", capabilities=[]), ["mcp__caps__t"])
    assert "COVERAGE CLAIMS" in text
    assert "PRECISION DISCIPLINE" in text
    assert "COMPUTE the comparison" in text


def test_reviewer_contract_demands_enumeration_for_universal_criteria(tmp_path,
                                                                      monkeypatch):
    """'met' on an 'every X' criterion requires the sweep, not spot-checks of
    the listed items — the exact channel of the campaign's one false success."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    prompts: dict[str, str] = {}

    async def runner(_t, name, prompt, options):
        prompts[name] = prompt
        if name.startswith("check.r"):
            return "VERDICT: approved\nok"
        return "```deliverable\nthe work\n```"

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write",
                   deliverable=True),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], rounds=2)],
        success_criteria=["every discrepancy between documents is listed"])
    bb = Blackboard(tmp_path / "u.db", "univ1")
    orch = Orchestrator(session="univ1", manager=None, ledger=None,
                        registry=None, blackboard=bb, broker=None,
                        planner=planner, resolver=None, session_runner=runner,
                        tracer=NullTracer())
    try:
        asyncio.run(orch.run("reconcile the documents"))
    finally:
        bb.close()
    ctx = prompts.get("check.r1", "")
    assert "UNIVERSAL CRITERIA" in ctx
    assert "enumeration argument" in ctx
    assert "false-success channel" in ctx
