"""Self-repair must see the REAL error.

The RAG post-mortem showed the developer agent repairing blind: error feedback
was truncated from the wrong end (pip/build errors and Python tracebacks live at
the END). These tests pin the fix.
"""
from __future__ import annotations

import asyncio

import pytest

from brain import acquisition, config
from brain.approval import ApprovalBroker
from brain.acquisition import _error_tail
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
        client = docker.DockerClient(base_url=config.DOCKER_HOST) if config.DOCKER_HOST else docker.from_env()
        client.ping()
        client.close()
        return True
    except Exception:  # noqa: BLE001
        return False


DOCKER = _docker_available()


def test_error_tail_keeps_the_end():
    text = ("boring preamble " * 500) + "ERROR: the real cause"
    tail = _error_tail(text, 100)
    assert tail.endswith("ERROR: the real cause")
    assert len(tail) <= 101  # limit + ellipsis
    # Short text passes through untouched.
    assert _error_tail("short", 100) == "short"


@pytest.mark.skipif(not DOCKER, reason="Docker daemon not reachable")
def test_repair_receives_pip_error_tail_and_previous_dockerfile(tmp_path):
    """Attempt 1 has a Dockerfile with a nonexistent pip package -> build fails.
    The 2nd developer call must receive (a) the actual pip ERROR line and (b) the
    previous Dockerfile; the full build log must be saved as a run artifact."""
    from brain.observability import Tracer
    from brain.sandbox.manager import SandboxManager

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    ledger = Ledger(tmp_path / "l.db")
    tracer = Tracer(tmp_path / "t.db", tmp_path / "runs")
    run = tracer.start_run("repair sees the error")
    mgr = SandboxManager("repair-test", ledger, tracer=tracer)
    broker = ApprovalBroker(auto="deny")

    bad_dockerfile = (
        "FROM python:3.12-slim\n"
        "RUN pip install --no-cache-dir nonexistent-package-xyzzy-42\n"
        "WORKDIR /app\nCOPY tool.py /app/tool.py\n"
    )
    good_dockerfile = "FROM python:3.12-slim\nWORKDIR /app\nCOPY tool.py /app/tool.py\n"
    program = "import json\nprint(json.dumps({'ok': True}))\n"

    seen: list[tuple] = []  # (previous, error) per developer call

    async def stub_developer(need_key, description, previous=None, error=None):
        seen.append((previous, error))
        dockerfile = bad_dockerfile if len(seen) == 1 else good_dockerfile
        return {
            "program": program,
            "dockerfile": dockerfile,
            "command": ["python", "-B", "/app/tool.py"],
            "input_schema": {"type": "object"},
            "env_map": {},
            "network": "none",
            "sample_input": {},
            "requires_credential": None,
        }

    try:
        cap = asyncio.run(acquisition.acquire(
            "repair_probe", manager=mgr, registry=reg, broker=broker,
            developer=stub_developer, description="probe", tracer=tracer,
        ))
        assert cap.verified  # second attempt succeeded

        # The 2nd call saw the REAL pip error (tail, not the apt-get head)...
        previous2, error2 = seen[1]
        assert error2 and "nonexistent-package-xyzzy-42" in error2
        assert "ERROR" in error2
        # ...and the previous Dockerfile for context.
        assert isinstance(previous2, dict)
        assert "nonexistent-package-xyzzy-42" in previous2.get("dockerfile", "")

        # Full build log persisted as a run artifact for post-mortems.
        log = (tmp_path / "runs" / run.run_id / "artifacts" / "repair_probe"
               / "attempt-1-author" / "build.log")
        assert log.exists() and "nonexistent-package-xyzzy-42" in log.read_text()

        assert ledger.find_leaks("repair-test") == []
    finally:
        mgr.close()
        ledger.close()
        reg.close()
        tracer.close()
