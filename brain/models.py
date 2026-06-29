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


@dataclass
class Task:
    id: str
    description: str
    capability_need: Optional[str]            # canonical capability key, or None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Plan:
    objective: str
    tasks: list[Task]


# Resolver decision kinds.
REUSE = "reuse"
CREATE = "create"
NOOP = "noop"


@dataclass
class Decision:
    kind: str                                  # REUSE | CREATE | NOOP
    capability_need: Optional[str]
    capability: Optional[Capability] = None    # populated when kind == REUSE
