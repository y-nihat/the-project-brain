"""Central configuration: paths, auth invariant, and default resource limits.

Importing this module is side-effect-light: it loads `.env` if present but does
NOT validate auth (so tests that only need Docker can import it). Call
``enforce_auth_invariant()`` explicitly at process startup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # python-dotenv is a convenience; env vars may already be set.
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False

# Project root = parent of the `brain` package directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "brain.db"
CAPABILITIES_STORE = DATA_DIR / "capabilities"   # promoted reusable tools (an output, not engine)
RUNS_DIR = DATA_DIR / "runs"          # per-run trace exports + created-tool artifacts
INBOX_DIR = DATA_DIR / "inbox"        # sanctioned drop folder: files named here get attached

# Engine version stamped onto every traced run (for reproducibility/regression)
# AND onto capability validation stamps (as part of acquisition.validation_epoch(),
# which also fingerprints the authoring contract + probe suite so a contract
# change invalidates promoted capabilities even without a release bump).
# KEEP IN LOCKSTEP WITH THE RELEASE TAG — a stale label here let a biased reader
# skip revalidation for a whole release cycle.
ENGINE_VERSION = "0.16.0"


def _git_commit() -> str:
    """Best-effort git commit for trace auditability (report: trace said 0.2.0
    while the checkout was v0.2.2 — version constants alone are not enough)."""
    try:
        import subprocess

        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


GIT_COMMIT = _git_commit()

# Load .env from the project root if present (no-op when missing).
load_dotenv(PROJECT_ROOT / ".env")

# Docker daemon endpoint. In the compose topology this points at the
# socket-proxy; left unset, the Docker SDK falls back to the default socket.
DOCKER_HOST = os.environ.get("DOCKER_HOST") or None

# Base image for sandboxed capabilities.
SANDBOX_BASE_IMAGE = os.environ.get("SANDBOX_BASE_IMAGE", "python:3.12-slim")

# Optional model override for orchestrator-side reasoning (None => SDK/CLI default).
BRAIN_MODEL = os.environ.get("BRAIN_MODEL") or None

# Model tiers (each falls back to BRAIN_MODEL, then the SDK default).
# STRONG carries judgment-heavy roles: planning, synthesis, review, tool authoring.
# FAST carries volume roles: map workers doing bounded per-shard extraction.
# e.g. BRAIN_MODEL_STRONG=claude-opus-4-8  BRAIN_MODEL_FAST=claude-sonnet-5
BRAIN_MODEL_STRONG = os.environ.get("BRAIN_MODEL_STRONG") or None
BRAIN_MODEL_FAST = os.environ.get("BRAIN_MODEL_FAST") or None


def model_for(role: str):
    """Pick the model tier for an engine role: high-frequency, small-context
    roles ('worker', 'supervisor') and tightly-scoped mechanics ('repair' — a
    seeded fix with the live traceback in hand) -> fast; judgment-heavy roles
    (planning, strategy adjudication, 'persistence' failure-boundary decisions,
    'research', authoring, synthesis) -> strong."""
    if role in ("worker", "supervisor", "repair"):
        return BRAIN_MODEL_FAST or BRAIN_MODEL
    return BRAIN_MODEL_STRONG or BRAIN_MODEL


class ConfigError(RuntimeError):
    """Raised when the runtime environment violates a hard invariant."""


def enforce_auth_invariant() -> None:
    """Refuse to start unless auth is OAuth-only.

    ``ANTHROPIC_API_KEY`` silently overrides ``CLAUDE_CODE_OAUTH_TOKEN``, so its
    mere presence is a misconfiguration we fail loudly on rather than guess.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise ConfigError(
            "ANTHROPIC_API_KEY is set and would override CLAUDE_CODE_OAUTH_TOKEN.\n"
            "Unset it before running the brain:  unset ANTHROPIC_API_KEY"
        )
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise ConfigError(
            "CLAUDE_CODE_OAUTH_TOKEN is not set. Run `claude setup-token` and put "
            "it in .env (see .env.example)."
        )


@dataclass(frozen=True)
class ResourceDefaults:
    """Conservative default limits applied to every sandbox container."""

    mem: str = "256m"
    cpus: float = 0.5
    pids: int = 128
    network: str = "none"
    ttl_seconds: int = 120
    read_only: bool = True


RESOURCE_DEFAULTS = ResourceDefaults()

# Engine ceilings for per-capability resource declarations. A tool may declare
# what it needs; the engine clamps to these — never above, never unbounded.
SANDBOX_MIN_MEM_MB = 64
SANDBOX_MAX_MEM_MB = int(os.environ.get("SANDBOX_MAX_MEM_MB", "2048"))
SANDBOX_MAX_CPUS = float(os.environ.get("SANDBOX_MAX_CPUS", "2.0"))
SANDBOX_MAX_TIMEOUT = int(os.environ.get("SANDBOX_MAX_TIMEOUT", "300"))
# Cap on files captured back from /work/out (protects the orchestrator process).
SANDBOX_MAX_OUTPUT_MB = int(os.environ.get("SANDBOX_MAX_OUTPUT_MB", "128"))

# Multimodal observation: image files a tool produced are returned to the agent
# as image content blocks (perception, not bulk transfer) — bounded per call.
OBSERVATION_MAX_IMAGES = int(os.environ.get("OBSERVATION_MAX_IMAGES", "3"))
OBSERVATION_MAX_IMAGE_BYTES = int(os.environ.get("OBSERVATION_MAX_IMAGE_BYTES",
                                                 str(2 * 1024 * 1024)))
# The Agent SDK's stdio transport rejects any single JSON message over 1 MiB —
# a tool result assembled above that limit kills the WHOLE agent session
# mid-call (observed: rendered-page images killed three stages including a
# final review round). Every result — text plus encoded images — must fit
# under this budget; images are recompressed/downscaled to fit, never allowed
# to exceed it. Headroom covers the JSON envelope and string escaping.
TRANSPORT_SAFE_RESULT_BYTES = int(os.environ.get("TRANSPORT_SAFE_RESULT_BYTES",
                                                 "700000"))
# The frame limit applies to the MESSAGE, and the CLI delivers all of one
# assistant turn's PARALLEL tool results in a single message — results that
# are individually under budget kill the session together (observed: a
# rendered-page result plus a parallel image read, each in budget, summed
# past the frame limit and crashed a review round). Every per-result budget
# is therefore the frame budget divided by this expected share.
TRANSPORT_FRAME_SHARE = max(1, int(os.environ.get("TRANSPORT_FRAME_SHARE", "3")))


def transport_result_budget() -> int:
    """Per-tool-result byte budget: the frame budget split across the results
    that may share one SDK frame (parallel tool calls in a single turn)."""
    return TRANSPORT_SAFE_RESULT_BYTES // TRANSPORT_FRAME_SHARE
# Mid-run tool repairs triggered by an AGENT's defect report (a tool that runs
# but is broken — crashes, wrong output shape). Each repair re-runs the
# reporting stage once.
TOOL_DEFECT_REPAIRS_PER_RUN = int(os.environ.get("TOOL_DEFECT_REPAIRS_PER_RUN", "2"))

# Watchdogs for agent (LLM) sessions: abort when no SDK message arrives for
# AGENT_STALL_TIMEOUT seconds, or when a session exceeds AGENT_TOTAL_TIMEOUT.
# Partial-message streaming makes token generation count as activity, so the
# stall timer only fires on true silence.
AGENT_STALL_TIMEOUT = float(os.environ.get("AGENT_STALL_TIMEOUT", "180"))
AGENT_TOTAL_TIMEOUT = float(os.environ.get("AGENT_TOTAL_TIMEOUT", "1200"))
# Turn budget per agent session. Tool-looping stages (map workers fetching and
# retrying) need real room; 10 proved too tight in live runs.
AGENT_MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "24"))
# A failed map shard is retried this many times with a fresh session.
MAP_RETRY_FAILED_SHARDS = int(os.environ.get("MAP_RETRY_FAILED_SHARDS", "1"))

# Team channel: agents signal the orchestrator (mediated — never each other
# directly): ask a completed upstream stage a question, or report a blocker.
TEAM_MAX_QUESTIONS = int(os.environ.get("TEAM_MAX_QUESTIONS", "3"))       # per session
# The reviewer may demand missing evidence once; a courier session fetches it.
REVIEW_EVIDENCE_FETCHES = int(os.environ.get("REVIEW_EVIDENCE_FETCHES", "1"))
# A review round that dies of INFRASTRUCTURE (session crash — not a verdict)
# may be retried this many times per review stage without consuming a round:
# with a 1-round budget, one transport fault otherwise equals review death and
# the draft ships unverified with its critique unwritten.
REVIEW_CRASH_RETRIES = int(os.environ.get("REVIEW_CRASH_RETRIES", "1"))
# Bounded mid-run loop re-entries: when agents report an unmet OBSERVATION need
# (report_evidence_gap), the planner may EXTEND the running plan this many times.
REPLANS_PER_RUN = int(os.environ.get("REPLANS_PER_RUN", "1"))
# Research delegation: an agent that recognizes it lacks knowledge can ask the
# orchestrator to research a topic (mediated — the agent never searches inline).
# Shared per-run budget across ALL requesters (stage agents + developer agents);
# results are cached by topic so the same question is not re-researched.
RESEARCH_MAX_REQUESTS = int(os.environ.get("RESEARCH_MAX_REQUESTS", "4"))
RESEARCH_AGENT_MAX_TURNS = int(os.environ.get("RESEARCH_AGENT_MAX_TURNS", "10"))
# Failure-driven recoveries: when a stage FAILS, the controller converts the
# failure into a residual requirement and re-enters strategy adjudication,
# replacing the failed node. This is now a SAFETY CEILING — the supervisor's
# persistence judgment is the real governor (it stops earlier when progress
# plateaus or the user's input is needed), so the ceiling can be higher without
# risking a runaway; the per-run agent-session cap remains the hard cost rail.
RECOVERIES_PER_RUN = int(os.environ.get("RECOVERIES_PER_RUN", "5"))

# Executive supervision: an LLM judgment consulted at decision moments
# (pre-user-interaction, stage completions). Fail-open, budget-capped.
SUPERVISOR_ENABLED = os.environ.get("SUPERVISOR_ENABLED", "1").lower() not in ("0", "false", "no")
SUPERVISOR_MAX_CALLS = int(os.environ.get("SUPERVISOR_MAX_CALLS", "12"))
# Failure-boundary persistence decisions have their OWN budget (routine
# consultations must never starve the keep-trying/stop-and-ask/stop judgment)
# and route to the STRONG model tier — they are strategy, not mechanics.
PERSISTENCE_MAX_CALLS = int(os.environ.get("PERSISTENCE_MAX_CALLS", "6"))

# User-provided files (@mentions / attach): copied into the run workspace.
FILE_ATTACH_MAX_MB = int(os.environ.get("FILE_ATTACH_MAX_MB", "50"))
FILE_ATTACH_TOTAL_MAX_MB = int(os.environ.get("FILE_ATTACH_TOTAL_MAX_MB", "200"))
# When a file the user NAMED cannot be accessed, ask them for a correct path
# this many times before proceeding without it (interactive sessions only).
FILE_ASK_ATTEMPTS = int(os.environ.get("FILE_ASK_ATTEMPTS", "2"))

# Reflection loop: watch a trusted tool's live health and repair it if it degrades.
CAPABILITY_CALL_BUDGET = int(os.environ.get("CAPABILITY_CALL_BUDGET", "25"))          # per (agent, capability)
CAPABILITY_UNHEALTHY_CONSECUTIVE_EMPTY = int(os.environ.get("CAPABILITY_UNHEALTHY_CONSECUTIVE_EMPTY", "3"))
CAPABILITY_UNHEALTHY_MIN_CALLS = int(os.environ.get("CAPABILITY_UNHEALTHY_MIN_CALLS", "5"))
CAPABILITY_UNHEALTHY_EMPTY_RATIO = float(os.environ.get("CAPABILITY_UNHEALTHY_EMPTY_RATIO", "0.7"))
CAPABILITY_MAX_REPAIRS_PER_RUN = int(os.environ.get("CAPABILITY_MAX_REPAIRS_PER_RUN", "2"))

# Orchestration: staged workflows (single | map | reduce | review).
# A `map` stage fans out over the manifest its upstream stage produced; width is
# decided at RUN time from the data, clamped by these caps so a plan can never
# stampede (the "50 subagents for a simple query" failure mode).
MAP_DEFAULT_SHARD_SIZE = int(os.environ.get("MAP_DEFAULT_SHARD_SIZE", "5"))       # items per worker
MAP_MAX_WORKERS_PER_STAGE = int(os.environ.get("MAP_MAX_WORKERS_PER_STAGE", "12"))
WORKER_PARALLELISM = int(os.environ.get("WORKER_PARALLELISM", "3"))               # concurrent sessions
RUN_MAX_AGENT_SESSIONS = int(os.environ.get("RUN_MAX_AGENT_SESSIONS", "40"))      # hard per-run cap
REVIEW_MAX_ROUNDS = int(os.environ.get("REVIEW_MAX_ROUNDS", "2"))                 # critique/fix cycles
# Above this, a reduce consolidates hierarchically (batches, then a final merge).
REDUCE_MAX_INPUT_CHARS = int(os.environ.get("REDUCE_MAX_INPUT_CHARS", "100000"))
# Bounded attempts to auto-repair MECHANICAL artifact defects (truncation etc.)
# detected by the engine — separate budget from semantic review rounds (#19/#21).
ARTIFACT_REPAIRS = int(os.environ.get("ARTIFACT_REPAIRS", "2"))
# Largest artifact the repair prompt may embed WHOLE. A file over this is left
# to review rather than silently clipped: a 24k-char prefix labeled "CURRENT
# content" made the repairer re-emit a truncated file — the exact defect the
# mechanism exists to fix.
REPAIR_MAX_CONTENT_CHARS = int(os.environ.get("REPAIR_MAX_CONTENT_CHARS", "120000"))
# Longest dependency chain at/above which the engine appends a review stage even
# without user evidence — very deep synthesis chains accumulate distortion (#13).
# (The primary trigger is user-evidence transformation, not depth.)
REVIEW_DEPTH_THRESHOLD = int(os.environ.get("REVIEW_DEPTH_THRESHOLD", "4"))
