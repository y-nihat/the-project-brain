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
from brain.models import CREATE, NOOP, REUSE, AgentSpec
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
        assert resolver.resolve("web_search").kind == CREATE
        # A verified capability in the registry -> REUSE.
        cap = acquisition.web_search_capability()
        cap.verified = True
        reg.add(cap)
        decision = resolver.resolve("web_search")
        assert decision.kind == REUSE and decision.capability.name == "web_search"
        # No capability need -> NOOP.
        assert resolver.resolve(None).kind == NOOP
    finally:
        reg.close()


def test_approval_gate_fires_when_tool_needs_credential(tmp_path):
    """The brain authors novel tools; it STOPS at the gate only when a tool
    fundamentally needs a credential/account/paid service (no free path)."""
    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    broker = ApprovalBroker(auto="deny")

    async def stub_developer(need_key, description, previous=None, error=None):
        return {
            "program": "print('x')",
            "dockerfile": "FROM python:3.12-slim",
            "command": ["python", "-B", "/app/tool.py"],
            "input_schema": {"type": "object"},
            "env_map": {},
            "network": "bridge",
            "sample_input": {},
            "requires_credential": "Acme API key",
        }

    try:
        with pytest.raises(acquisition.AcquisitionError):
            asyncio.run(acquisition.acquire(
                "premium_thing", manager=None, registry=reg, broker=broker, developer=stub_developer,
            ))
        # The gate was consulted exactly once and denied.
        assert len(broker.log) == 1 and broker.log[0][1] is False
    finally:
        reg.close()


@pytest.mark.skipif(not DOCKER, reason="Docker daemon not reachable")
def test_author_novel_tool_at_runtime(tmp_path):
    """The brain authors a NOVEL tool at runtime (developer writes Python) -> build
    -> generic verify -> promote, with the script captured under the run."""
    from brain.observability import Tracer
    from brain.sandbox.manager import SandboxManager

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    ledger = Ledger(tmp_path / "l.db")
    tracer = Tracer(tmp_path / "t.db", tmp_path / "runs")
    run = tracer.start_run("author echo_tool")
    mgr = SandboxManager("author", ledger, tracer=tracer)
    broker = ApprovalBroker(auto="deny")  # a free tool must NOT consult the gate

    program = "import json, os\nn = os.environ.get('N', '')\nprint(json.dumps({'echo': n}))\n"
    dockerfile = "FROM python:3.12-slim\nWORKDIR /app\nCOPY tool.py /app/tool.py\n"

    async def stub_developer(need_key, description, previous=None, error=None):
        return {
            "program": program,
            "dockerfile": dockerfile,
            "command": ["python", "-B", "/app/tool.py"],
            "input_schema": {"type": "object", "properties": {"n": {"type": "string"}}},
            "env_map": {"n": "N"},
            "network": "none",
            "sample_input": {"n": "hello"},
            "requires_credential": None,
        }

    try:
        cap = asyncio.run(acquisition.acquire(
            "echo_tool", manager=mgr, registry=reg, broker=broker,
            developer=stub_developer, description="echo the input string", tracer=tracer,
        ))
        assert cap.verified and reg.get("echo_tool").verified
        assert broker.log == []                       # free path => gate not consulted
        assert ledger.find_leaks("author") == []      # build + verify containers reaped
        promoted = tmp_path / "runs" / run.run_id / "artifacts" / "echo_tool" / "promoted" / "tool.py"
        assert promoted.exists()                       # authored script captured under the run
    finally:
        mgr.close()
        ledger.close()
        reg.close()
        tracer.close()


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
    planner = StubPlanner([
        AgentSpec(id="researcher", role="researcher",
                  goal="find what site reliability engineering is, using web search",
                  capabilities=["web_search"]),
        AgentSpec(id="writer", role="writer",
                  goal="write a two-sentence summary from the researcher's findings",
                  capabilities=[], depends_on=["researcher"]),
    ])
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
        answer = asyncio.run(orch.run("Explain site reliability engineering."))
        assert answer and "(no answer" not in answer
        assert reg.get("web_search").verified
        assert ledger.find_leaks("m1-e2e") == []
        # Both agents ran and the writer received the researcher's output (shared context).
        assert bb.get("output:researcher") and bb.get("output:writer")
        # Capability reused on a fresh resolve (no rebuild).
        assert resolver.resolve("web_search").kind == REUSE
    finally:
        mgr.close()
        ledger.close()
        reg.close()
        bb.close()
