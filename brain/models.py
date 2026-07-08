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
    # Name of the credential env var this tool fundamentally needs (e.g.
    # "SERVICE_API_KEY"). The VALUE lives only in the session's ApprovalBroker
    # and is injected at run time — never stored, never traced.
    requires_credential: Optional[str] = None
    # Human-facing instructions for OBTAINING that credential, written by the
    # tool's author. The engine shows this BEFORE prompting for the value — the
    # asker must deliver the help, whatever the plan shape.
    credential_help: Optional[str] = None


@dataclass
class AgentSpec:
    """One concrete agent session.

    Since the workflow rework, this is the *instantiated* unit: the orchestrator
    derives one AgentSpec per session from a :class:`StageSpec` (a `map` stage
    yields one per shard). The factory turns it into a locked, dynamically
    prompted session.
    """

    id: str
    role: str
    goal: str
    capabilities: list[str] = field(default_factory=list)   # capability keys this agent needs
    depends_on: list[str] = field(default_factory=list)      # ids of upstream agents/stages


# Stage execution patterns.
SINGLE = "single"    # one agent, one session (the degenerate workflow)
MAP = "map"          # replicate a worker template over runtime-determined shards
REDUCE = "reduce"    # consolidate upstream workers' notes into one output
REVIEW = "review"    # verify the upstream draft against evidence; demand fixes
STAGE_PATTERNS = (SINGLE, MAP, REDUCE, REVIEW)


@dataclass
class StageSpec:
    """One stage of a planned workflow.

    The planner designs a DAG of these; the orchestrator executes them in
    dependency order, deciding a `map` stage's fan-out width at run time from
    the manifest its upstream stage actually produced.
    """

    id: str
    pattern: str                                              # one of STAGE_PATTERNS
    role: str
    goal: str
    capabilities: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)       # ids of upstream stages
    shard_size: int = 0            # map: items per worker (0 => engine default)
    shard_from: Optional[str] = None  # map: stage whose manifest is sharded (default: first dep)
    rounds: int = 1                # review: max critique/fix cycles (clamped)


@dataclass
class Plan:
    objective: str
    stages: list[StageSpec]


# Resolver decision kinds.
REUSE = "reuse"
CREATE = "create"
NOOP = "noop"


@dataclass
class Decision:
    kind: str                                  # REUSE | CREATE | NOOP
    capability_need: Optional[str]
    capability: Optional[Capability] = None    # populated when kind == REUSE
