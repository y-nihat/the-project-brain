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
CAPABILITIES_STORE = PROJECT_ROOT / "capabilities_store"

# Load .env from the project root if present (no-op when missing).
load_dotenv(PROJECT_ROOT / ".env")

# Docker daemon endpoint. In the compose topology this points at the
# socket-proxy; left unset, the Docker SDK falls back to the default socket.
DOCKER_HOST = os.environ.get("DOCKER_HOST") or None

# Base image for sandboxed capabilities.
SANDBOX_BASE_IMAGE = os.environ.get("SANDBOX_BASE_IMAGE", "python:3.12-slim")

# Optional model override for orchestrator-side reasoning (None => SDK/CLI default).
BRAIN_MODEL = os.environ.get("BRAIN_MODEL") or None


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
