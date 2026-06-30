"""Milestone 1 acceptance.

Layered by dependency so each runs in the strongest environment available:
  * resolver + approval-gate tests: pure, always run.
  * acquire/verify/promote: needs Docker (recipe path, no model token).
  * full end-to-end via the orchestrator: needs Docker + an OAuth token.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from brain import acquisition, config
from brain.approval import ApprovalBroker
from brain.ledger import Ledger
from brain.models import CREATE, NOOP, REUSE, Task
from brain.registry import Registry
from brain.resolver import Resolver

try:
    import docker
except Exception:  # noqa: BLE001
    docker = None


def _docker_available() -> bool:
    if docker is None:
        return False
    try:
        client = docker.DockerClient(base_url=config.DOCKER_HOST) if config.DOCKER_HOST else docker.from_env()
        client.ping()
        client.close()
        return True
    except Exception:  # noqa: BLE001
        return False


DOCKER = _docker_available()
TOKEN = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")) and not os.environ.get("ANTHROPIC_API_KEY")


# --- always run: pure logic ------------------------------------------------ #
def test_resolver_reuse_vs_create(tmp_path):
    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    resolver = Resolver(reg)
    try:
        # Unknown capability -> CREATE.
        assert resolver.resolve(Task("t1", "x", "web_search")).kind == CREATE
        # A verified capability in the registry -> REUSE.
        cap = acquisition.web_search_capability()
        cap.verified = True
        reg.add(cap)
        decision = resolver.resolve(Task("t1", "x", "web_search"))
        assert decision.kind == REUSE and decision.capability.name == "web_search"
        # No capability need -> NOOP.
        assert resolver.resolve(Task("t2", "x", None)).kind == NOOP
    finally:
        reg.close()


def test_approval_gate_blocks_when_no_free_path(tmp_path):
    """A need with no known free, keyless recipe must STOP at the approval gate."""
    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    broker = ApprovalBroker(auto="deny")
    try:
        with pytest.raises(acquisition.AcquisitionError):
            asyncio.run(
                acquisition.acquire("proprietary_thing", manager=None, registry=reg, broker=broker)
            )
        # The gate was consulted exactly once and the request was denied.
        assert len(broker.log) == 1
        assert broker.log[0][1] is False
    finally:
        reg.close()


# --- needs Docker: build + verify + promote (free path, no model) ---------- #
@pytest.mark.skipif(not DOCKER, reason="Docker daemon not reachable")
def test_acquire_verify_promote_web_search(tmp_path):
    import json
    import sqlite3

    from brain.observability import Tracer
    from brain.sandbox.manager import SandboxManager

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    ledger = Ledger(tmp_path / "l.db")
    tracer = Tracer(tmp_path / "t.db", tmp_path / "runs")
    run = tracer.start_run("acquire web_search")
    mgr = SandboxManager("m1-acq", ledger, tracer=tracer)
    broker = ApprovalBroker(auto="deny")  # must NOT be consulted for a free path
    try:
        cap = asyncio.run(
            acquisition.acquire(
                "web_search", manager=mgr, registry=reg, broker=broker, builder=None, tracer=tracer
            )
        )
        assert cap.verified
        stored = reg.get("web_search")
        assert stored is not None and stored.verified
        assert broker.log == []  # free, keyless path => gate never fired
        assert ledger.find_leaks("m1-acq") == []  # build + verify containers reaped

        # The created tool's scripts were captured as outputs of this run.
        promoted = tmp_path / "runs" / run.run_id / "artifacts" / "web_search" / "promoted"
        assert (promoted / "search.py").exists()
        assert (promoted / "Dockerfile").exists()

        # The promote event records where the code lives (canonical + run snapshot).
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT data_json FROM events WHERE kind='capability.promote'"
        ).fetchall()
        conn.close()
        data = json.loads(rows[-1]["data_json"])
        assert data.get("code_path") and data.get("artifact_path")
    finally:
        mgr.close()
        ledger.close()
        reg.close()
        tracer.close()


# --- needs Docker + OAuth token: full slice via orchestrator --------------- #
@pytest.mark.skipif(not (DOCKER and TOKEN), reason="needs Docker + CLAUDE_CODE_OAUTH_TOKEN")
def test_m1_end_to_end(tmp_path):
    from brain.memory.blackboard import Blackboard
    from brain.orchestrator import Orchestrator
    from brain.planner import StubPlanner
    from brain.sandbox.manager import SandboxManager

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    ledger = Ledger(tmp_path / "l.db")
    bb = Blackboard(tmp_path / "b.db", "m1-e2e")
    mgr = SandboxManager("m1-e2e", ledger)
    broker = ApprovalBroker(auto="deny")
    planner = StubPlanner([Task("t1", "find live info", "web_search")])
    resolver = Resolver(reg)
    orch = Orchestrator(
        session="m1-e2e",
        manager=mgr,
        ledger=ledger,
        registry=reg,
        blackboard=bb,
        broker=broker,
        planner=planner,
        resolver=resolver,
        model=config.BRAIN_MODEL,
        use_builder=False,  # deterministic recipe path for the test
    )
    try:
        answer = asyncio.run(orch.run("What is site reliability engineering? Use web search."))
        assert answer and "(no answer" not in answer
        assert reg.get("web_search").verified
        assert ledger.find_leaks("m1-e2e") == []
        # Second time around, the capability is reused (no rebuild).
        assert resolver.resolve(Task("t1", "x", "web_search")).kind == REUSE
    finally:
        mgr.close()
        ledger.close()
        reg.close()
        bb.close()
