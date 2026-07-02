"""Resource and network policy for sandbox containers.

A ``ResourcePolicy`` is the single place that decides the limits and isolation
flags handed to Docker. The defaults are deliberately strict (no network,
read-only rootfs, all capabilities dropped); a capability that genuinely needs
network egress opts into :func:`egress`.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import config

# Network modes.
NETWORK_NONE = "none"      # fully isolated; no outbound access
NETWORK_EGRESS = "bridge"  # default bridge => outbound internet allowed


@dataclass
class ResourcePolicy:
    mem: str = config.RESOURCE_DEFAULTS.mem
    cpus: float = config.RESOURCE_DEFAULTS.cpus
    pids: int = config.RESOURCE_DEFAULTS.pids
    network: str = NETWORK_NONE
    ttl_seconds: int = config.RESOURCE_DEFAULTS.ttl_seconds
    read_only: bool = config.RESOURCE_DEFAULTS.read_only
    cap_drop: tuple[str, ...] = ("ALL",)
    security_opt: tuple[str, ...] = ("no-new-privileges:true",)

    def docker_kwargs(self) -> dict:
        """Translate the policy into keyword args for docker-py's containers API."""
        return {
            "mem_limit": self.mem,
            # Hard cap: without this Docker grants an equal amount of swap, so a
            # "256m" container can silently use ~512m at unpredictable speed.
            "memswap_limit": self.mem,
            "nano_cpus": int(self.cpus * 1_000_000_000),
            "pids_limit": self.pids,
            "network_mode": self.network,
            "read_only": self.read_only,
            "cap_drop": list(self.cap_drop),
            "security_opt": list(self.security_opt),
            # Writable scratch so a read-only rootfs can still run a process.
            "tmpfs": {"/tmp": "size=64m,mode=1777", "/work": "size=64m,mode=0777"},
        }


def isolated() -> ResourcePolicy:
    """Default policy: no network."""
    return ResourcePolicy(network=NETWORK_NONE)


def egress() -> ResourcePolicy:
    """Policy for capabilities that declared a need for outbound internet."""
    return ResourcePolicy(network=NETWORK_EGRESS)


def for_capability(
    memory_mb=None,
    cpus=None,
    timeout_seconds=None,
    network: str = NETWORK_NONE,
) -> ResourcePolicy:
    """Policy honoring a capability's declared needs, clamped to engine ceilings.

    Undeclared values keep the strict defaults; declared values are bounded to
    [SANDBOX_MIN_MEM_MB, SANDBOX_MAX_MEM_MB] / SANDBOX_MAX_CPUS / SANDBOX_MAX_TIMEOUT
    so no tool can request unbounded resources.
    """
    pol = ResourcePolicy(network=NETWORK_EGRESS if network == "bridge" else NETWORK_NONE)
    if memory_mb:
        mem_mb = max(config.SANDBOX_MIN_MEM_MB, min(int(memory_mb), config.SANDBOX_MAX_MEM_MB))
        pol.mem = f"{mem_mb}m"
    if cpus:
        pol.cpus = max(0.1, min(float(cpus), config.SANDBOX_MAX_CPUS))
    if timeout_seconds:
        pol.ttl_seconds = max(10, min(int(timeout_seconds), config.SANDBOX_MAX_TIMEOUT))
    return pol
