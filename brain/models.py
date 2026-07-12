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
    # Procedural-memory immune system (#9): which engine version last validated
    # this capability against the probe battery, and why it was quarantined (a
    # quarantined capability is never reused; the brain re-authors instead).
    validated_engine: Optional[str] = None
    quarantined: Optional[str] = None
    # Applicability contract (#11): file extensions this tool can actually
    # process. The engine refuses to execute it on anything else — a tool never
    # gets the CHANCE to substitute a stimulus for an incompatible input.
    accepts: list[str] = field(default_factory=list)
    # Epistemic output role, DECLARED by the tool's contract — never inferred
    # from its name or inputs: "observation" (verbatim extraction from given
    # inputs), "derived" (interpretation/synthesis), "artifact" (a produced
    # deliverable), or "validation" (a check result). Only "observation"
    # output may be treated as original source evidence downstream — consuming
    # a file does NOT imply producing source evidence (validators do too).
    emits: str = ""


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
    # Objective-derived success contract: checkable criteria compiled from the
    # user's request (e.g. "exactly 3 sections", "claims traceable to the source
    # file", "final answer accurately describes the artifact"). Generated per
    # objective — never a fixed template — and used by review/supervision.
    success_criteria: list[str] = field(default_factory=list)
    # Why THIS strategy: the planner's comparison of candidate observation
    # methods. Persisted and traced — an unauditable initial method choice is
    # how a familiar mechanism silently substitutes for the required one.
    strategy_rationale: str = ""
    # Per NEW capability key: an instance-neutral authoring contract plus the
    # concrete example inputs for verification. Separated from the stage goal
    # so task policy (a URL, "one paragraph", synthesis instructions) never
    # compiles into reusable mechanism code: {key: {contract, example_input}}.
    capability_needs: dict = field(default_factory=dict)


@dataclass
class MethodOutcome:
    """A method/stage failure converted into controller state — the observation
    the adaptive loop reasons over. Produced automatically at the stage
    boundary; never dependent on an agent volunteering a report (the agent may
    not exist: capability acquisition fails before any agent starts)."""

    stage_id: str
    requirement: str        # what the stage was to establish (its goal) — stays open
    phase: str              # "acquisition" | "execution"
    failure_class: str      # authority | packaging | implementation | contract |
                            # submission | suppressed | budget | engine | ...
    detail: str             # concise causal account (the END of the error)
    capability: str = ""    # the capability need involved, for acquisition failures
    attempts: int = 0       # implementation attempts already spent on the method
    attempt_history: list[str] = field(default_factory=list)  # one line per attempt
    retryable: bool = True  # False when same-method retry cannot help (authority, budget)
    partial_evidence: list[str] = field(default_factory=list)  # workspace files retained


# Resolver decision kinds.
REUSE = "reuse"
CREATE = "create"
NOOP = "noop"


@dataclass
class Decision:
    kind: str                                  # REUSE | CREATE | NOOP
    capability_need: Optional[str]
    capability: Optional[Capability] = None    # populated when kind == REUSE
