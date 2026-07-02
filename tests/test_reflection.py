"""The reflection loop: result classification, health monitor, runtime repair."""
from __future__ import annotations

import asyncio
import json

import pytest

from brain import acquisition, config
from brain.approval import ApprovalBroker
from brain.capabilities import health
from brain.capabilities.health import HealthMonitor
from brain.ledger import Ledger
from brain.registry import Registry

try:
    import docker
except Exception:  # noqa: BLE001
    docker = None


def _docker_available() -> bool:
    if docker is None:
        return False
    try:
        c = docker.DockerClient(base_url=config.DOCKER_HOST) if config.DOCKER_HOST else docker.from_env()
        c.ping(); c.close(); return True
    except Exception:  # noqa: BLE001
        return False


DOCKER = _docker_available()


# --- classification -------------------------------------------------------- #
def test_classify_result():
    assert health.classify_result(json.dumps({"query": "x", "results": []}))[0] == health.EMPTY
    ok, n = health.classify_result(json.dumps({"query": "x", "results": [{"t": 1}, {"t": 2}]}))
    assert ok == health.OK and n == 2
    assert health.classify_result(json.dumps({"error": "boom"}))[0] == health.ERROR
    assert health.classify_result("")[0] == health.EMPTY
    assert health.classify_result("just some prose, not json")[0] == health.OK  # unstructured: not judged
    assert health.classify_result("x", is_error=True)[0] == health.ERROR


# --- monitor: unhealthy triggers + budget ---------------------------------- #
def test_monitor_flags_consecutive_empties():
    m = HealthMonitor()
    for _ in range(2):
        m.record("web_search", health.EMPTY)
    assert m.unhealthy("web_search") is None            # below the consecutive threshold (3)
    m.record("web_search", health.EMPTY)
    assert "row" in (m.unhealthy("web_search") or "")   # 3 in a row -> unhealthy
    m.record("web_search", health.OK)
    assert m.unhealthy("web_search") is None            # a good result resets the streak


def test_monitor_flags_high_empty_ratio():
    m = HealthMonitor()
    # 4 empty, 1 ok, 1 empty -> alternating avoids 3-in-a-row but ratio is high
    for cls in [health.EMPTY, health.OK, health.EMPTY, health.EMPTY, health.OK, health.EMPTY]:
        m.record("t", cls)
    # 4/6 = 67% < 70%; add one more empty -> 5/7 = 71%
    m.record("t", health.EMPTY)
    assert "not delivering" in (m.unhealthy("t") or "")


def test_monitor_budget():
    m = HealthMonitor(call_budget=3)
    assert not m.over_budget("t")
    for _ in range(3):
        m.record("t", health.OK)
    assert m.over_budget("t")


# --- repair (Docker, stub developer) --------------------------------------- #
@pytest.mark.skipif(not DOCKER, reason="Docker daemon not reachable")
def test_repair_capability_reauthors_from_seed(tmp_path):
    from brain.observability import Tracer
    from brain.sandbox.manager import SandboxManager

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    ledger = Ledger(tmp_path / "l.db")
    tracer = Tracer(tmp_path / "t.db", tmp_path / "runs")
    tracer.start_run("repair")
    mgr = SandboxManager("repair-test", ledger, tracer=tracer)
    broker = ApprovalBroker(auto="deny")

    # A tool that always returns empty (the degraded starting point).
    empty = ("import json\nprint(json.dumps({'query': 'x', 'results': []}))\n")
    dockerfile = "FROM python:3.12-slim\nWORKDIR /app\nCOPY tool.py /app/tool.py\n"
    calls = {"n": 0}

    async def stub_developer(need_key, description, previous=None, error=None):
        calls["n"] += 1
        # First (author) submits the empty tool; the repair call gets a working one.
        prog = empty if previous is None else (
            "import json\nprint(json.dumps({'query': 'x', 'results': [{'title': 'a'}]}))\n")
        return {"program": prog, "dockerfile": dockerfile, "command": ["python", "-B", "/app/tool.py"],
                "input_schema": {"type": "object"}, "env_map": {}, "network": "none",
                "sample_input": {}, "requires_credential": None}

    try:
        cap = asyncio.run(acquisition.acquire(
            "flaky_search", manager=mgr, registry=reg, broker=broker,
            developer=stub_developer, description="search", tracer=tracer))
        assert cap.verified  # promoted even though it returns empty (verify only checks shape)

        repaired = asyncio.run(acquisition.repair_capability(
            cap, "returned empty 3 times in a row", manager=mgr, registry=reg, broker=broker,
            developer=stub_developer, tracer=tracer))
        assert repaired.verified
        # The repair was seeded with the previous code (developer saw `previous`).
        assert calls["n"] >= 2
        # Version bumped for provenance.
        assert reg.get("flaky_search").version == "1.0.1"
        assert ledger.find_leaks("repair-test") == []
    finally:
        mgr.close(); ledger.close(); reg.close(); tracer.close()
