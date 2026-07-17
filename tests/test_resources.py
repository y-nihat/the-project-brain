"""Resource management: hard caps, declared needs, OOM detection, accounting.

Pins the fixes for the observed memory inconsistencies: swap silently doubling
limits, one-size-fits-all limits, invisible OOM kills, and unbounded output
capture.
"""
from __future__ import annotations

import sqlite3

import pytest

from brain import config
from brain.ledger import Ledger
from brain.models import Capability
from brain.registry import Registry
from brain.sandbox import policy

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


# --- pure: policy ----------------------------------------------------------- #
def test_memory_limit_is_hard_no_swap():
    kwargs = policy.isolated().docker_kwargs()
    assert kwargs["memswap_limit"] == kwargs["mem_limit"]


def test_host_service_doorway_only_for_networked_tools():
    """A networked tool can reach HOST-published services via
    host.docker.internal (needed for a run's local device/API simulator) —
    strictly narrower than its existing internet egress, and it never places
    the tool on the brain's network, so the socket-proxy stays unreachable.
    An isolated tool gets no such doorway."""
    net = policy.egress().docker_kwargs()
    assert net["extra_hosts"] == {"host.docker.internal": "host-gateway"}
    assert net["network_mode"] == "bridge"
    iso = policy.isolated().docker_kwargs()
    assert "extra_hosts" not in iso
    assert iso["network_mode"] == "none"


def test_for_capability_clamps_to_ceilings():
    pol = policy.for_capability(memory_mb=999999, cpus=64, timeout_seconds=99999, network="bridge")
    assert pol.mem == f"{config.SANDBOX_MAX_MEM_MB}m"
    assert pol.cpus == config.SANDBOX_MAX_CPUS
    assert pol.ttl_seconds == config.SANDBOX_MAX_TIMEOUT
    assert pol.network == "bridge"
    # Undeclared -> strict defaults.
    default = policy.for_capability()
    assert default.mem == config.RESOURCE_DEFAULTS.mem
    assert default.cpus == config.RESOURCE_DEFAULTS.cpus
    assert default.network == "none"
    # Declared within bounds -> honored.
    pol2 = policy.for_capability(memory_mb=768)
    assert pol2.mem == "768m"


# --- pure: schema migrations ------------------------------------------------ #
def test_registry_migrates_old_schema_and_stores_resources(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE capabilities (name TEXT PRIMARY KEY, version TEXT, description TEXT, "
        "image_tag TEXT, command TEXT, input_schema TEXT, env_map TEXT, network TEXT, "
        "tags TEXT, verified INTEGER, created_at TEXT)"
    )
    conn.commit()
    conn.close()

    reg = Registry(db, tmp_path / "store")  # must ALTER in the new columns
    try:
        cap = Capability(
            name="thing", description="d", image_tag="t:1", command=["x"],
            input_schema={}, env_map={}, memory_mb=768, cpus=1.5, timeout_seconds=200,
        )
        reg.add(cap)
        got = reg.get("thing")
        assert (got.memory_mb, got.cpus, got.timeout_seconds) == (768, 1.5, 200)
    finally:
        reg.close()


def test_ledger_migrates_and_records_oom_peak(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE containers (id TEXT PRIMARY KEY, session TEXT NOT NULL, capability TEXT, "
        "image TEXT, command TEXT, mem TEXT, cpus REAL, pids INTEGER, network TEXT, ttl INTEGER, "
        "status TEXT NOT NULL, exit_code INTEGER, started_at TEXT NOT NULL, finished_at TEXT)"
    )
    conn.commit()
    conn.close()

    led = Ledger(db)
    try:
        led.record_start(id="c1", session="s", capability=None, image="i", command=["x"],
                         policy=policy.isolated())
        led.record_finish("c1", exit_code=137, status="finished", peak_mem_mb=250.5, oom=True)
        row = led.all("s")[0]
        assert row["oom"] == 1 and row["peak_mem_mb"] == 250.5
    finally:
        led.close()


# --- docker: OOM detection + declared resources ----------------------------- #
@pytest.mark.skipif(not DOCKER, reason="Docker daemon not reachable")
def test_oom_is_detected_and_declared_memory_fixes_it(tmp_path):
    from brain.sandbox.manager import SandboxManager

    # Gradually allocate ~600 MB: killed under the 256 MB default, fine under 768 MB.
    grow = ("import sys\n"
            "blocks = []\n"
            "for i in range(60):\n"
            "    blocks.append(b'x' * (10 * 1024 * 1024))\n"
            "print('survived', len(blocks))\n")

    ledger = Ledger(tmp_path / "l.db")
    mgr = SandboxManager("oom-test", ledger)
    try:
        killed = mgr.run_ephemeral(
            "python:3.12-slim", ["python", "-c", grow], policy=policy.isolated(),
            capability="oom_probe",
        )
        assert killed.oom is True
        assert killed.exit_code == 137
        assert not killed.ok

        ok = mgr.run_ephemeral(
            "python:3.12-slim", ["python", "-c", grow],
            policy=policy.for_capability(memory_mb=768),
            capability="oom_probe",
        )
        assert ok.ok and "survived" in ok.stdout
        assert ok.oom is False

        rows = ledger.all("oom-test")
        assert rows[0]["oom"] == 1 and rows[1]["oom"] == 0
        assert ledger.find_leaks("oom-test") == []
    finally:
        mgr.close()
        ledger.close()


@pytest.mark.skipif(not DOCKER, reason="Docker daemon not reachable")
def test_peak_memory_is_recorded(tmp_path):
    from brain.sandbox.manager import SandboxManager

    hold = ("import time\n"
            "b = b'x' * (120 * 1024 * 1024)\n"
            "time.sleep(3)\n"
            "print('held', len(b))\n")
    ledger = Ledger(tmp_path / "l.db")
    mgr = SandboxManager("peak-test", ledger)
    try:
        res = mgr.run_ephemeral(
            "python:3.12-slim", ["python", "-c", hold],
            policy=policy.for_capability(memory_mb=512), capability="peak_probe",
        )
        assert res.ok
        # Best-effort sampler: the 3s hold guarantees at least one sample.
        assert res.peak_mem_mb is not None and res.peak_mem_mb > 100
    finally:
        mgr.close()
        ledger.close()
