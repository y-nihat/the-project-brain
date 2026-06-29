"""Phase 0 acceptance: a locked-down sandbox round-trip with no leaks.

Spins one `--network none`, CPU/memory-limited container, runs `echo`, captures
the output, and proves the container is gone and the ledger is leak-free.

Skipped automatically when no Docker daemon is reachable.
"""
from __future__ import annotations

import docker
import pytest

from brain import config
from brain.ledger import Ledger
from brain.sandbox import policy
from brain.sandbox.manager import SandboxManager

SESSION = "phase0-smoke"


def _docker_available() -> bool:
    try:
        client = docker.DockerClient(base_url=config.DOCKER_HOST) if config.DOCKER_HOST else docker.from_env()
        client.ping()
        client.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="Docker daemon not reachable")


def test_locked_down_sandbox_roundtrip(tmp_path):
    ledger = Ledger(tmp_path / "smoke.db")
    mgr = SandboxManager(SESSION, ledger)
    try:
        result = mgr.run_ephemeral(
            "busybox:latest",
            ["echo", "hello-from-sandbox"],
            policy=policy.isolated(),  # network: none
            capability="smoke",
        )

        # Ran successfully and we captured its output.
        assert result.exit_code == 0, result.stderr
        assert "hello-from-sandbox" in result.stdout
        assert not result.timed_out

        # Container is gone (teardown happened).
        leftovers = mgr.client.containers.list(
            all=True, filters={"label": f"brain.session={SESSION}"}
        )
        assert leftovers == []

        # Ledger recorded exactly one finished container and reports no leaks.
        rows = ledger.all(SESSION)
        assert len(rows) == 1
        assert rows[0]["status"] == "finished"
        assert rows[0]["network"] == "none"
        assert ledger.find_leaks(SESSION) == []
    finally:
        mgr.close()
        ledger.close()
