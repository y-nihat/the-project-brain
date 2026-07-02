"""Shared data models, kept in one place to avoid import cycles."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Capability:
    """A registered, reusable unit the brain can run in a sandbox and expose to
    agents as the MCP tool ``mcp__caps__<name>``."""

    name: str
    description: str
    image_tag: str
    command: list[str]
    input_schema: dict          # JSON schema for the MCP tool
    env_map: dict               # tool-arg name -> container env var name
    network: str = "none"       # "none" | "bridge"
    version: str = "1.0.0"
    tags: list[str] = field(default_factory=list)
    verified: bool = False
    # Declared resource needs (clamped to engine ceilings at run time; None => defaults).
    memory_mb: Optional[int] = None
    cpus: Optional[float] = None
    timeout_seconds: Optional[int] = None


@dataclass
class AgentSpec:
    """One agent the brain decides to create for an objective.

    The planner designs a *roster* of these; the agent factory turns each into a
    concrete (dynamically-prompted) agent session, and the orchestrator runs them
    in dependency order.
    """

    id: str
    role: str
    goal: str
    capabilities: list[str] = field(default_factory=list)   # capability keys this agent needs
    depends_on: list[str] = field(default_factory=list)      # ids of upstream agents


@dataclass
class Plan:
    objective: str
    agents: list[AgentSpec]


# Resolver decision kinds.
REUSE = "reuse"
CREATE = "create"
NOOP = "noop"


@dataclass
class Decision:
    kind: str                                  # REUSE | CREATE | NOOP
    capability_need: Optional[str]
    capability: Optional[Capability] = None    # populated when kind == REUSE
