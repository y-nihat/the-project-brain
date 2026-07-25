"""Phase 0 acceptance: a locked-down sandbox round-trip with no leaks.

Spins one `--network none`, CPU/memory-limited container, runs `echo`, captures
the output, and proves the container is gone and the ledger is leak-free.

Skipped automatically when no Docker daemon is reachable.
"""
from __future__ import annotations

import docker
import pytest

from brain import config
from tests._sandbox import SANDBOX_READY, SANDBOX_SKIP_REASON
from brain.ledger import Ledger
from brain.sandbox import policy
from brain.sandbox.manager import SandboxManager

SESSION = "phase0-smoke"
pytestmark = pytest.mark.skipif(not SANDBOX_READY, reason=SANDBOX_SKIP_REASON)


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


def test_file_io_roundtrip(tmp_path):
    """A file slides in (/work/in), the tool transforms it, and the result comes
    back out (/work/out) — the 'loading door' for file-based tools."""
    ledger = Ledger(tmp_path / "fileio.db")
    mgr = SandboxManager("phase0-fileio", ledger)
    try:
        code = (
            "import os; os.makedirs('/work/out', exist_ok=True); "
            "open('/work/out/greeting.txt','w').write('hello ' + open('/work/in/name.txt').read())"
        )
        result = mgr.run_ephemeral(
            "python:3.12-slim",
            ["python", "-c", code],
            policy=policy.isolated(),  # network: none
            capability="fileio",
            input_files={"name.txt": b"world"},
            capture_output_files=True,
        )
        assert result.exit_code == 0, result.stderr
        assert result.output_files.get("greeting.txt") == b"hello world"
        assert ledger.find_leaks("phase0-fileio") == []
    finally:
        mgr.close()
        ledger.close()
