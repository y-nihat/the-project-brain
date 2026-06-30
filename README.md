# The Project Brain

A self-organizing agent **brain** built on the Claude Agent SDK. Give it an objective; it plans the
work, decides which agents/capabilities already exist vs. must be built, builds missing capabilities
**inside Docker sandboxes**, verifies they actually work, and promotes the reusable ones into a
growing capability library — under strict free-first, approval-gate, and full-isolation constraints.

> Status: Phase 0 (safety substrate), Milestone 1 (web-search slice), and Observability
> (execution-history tracer) are **built and verified**. Runs today via docker compose
> (not yet a `pip` package — that's milestone M2).
> See **[docs/OVERVIEW.md](docs/OVERVIEW.md)** for the stack, how to run it, and where logs live.

## Safety model (enforced structurally, not by trusting the model)

- **Nothing agent-generated runs on the host.** Agents are LLM sessions the orchestrator drives;
  their only way to act is a tool call. Agents get **no host `Bash`, no host file-write, and no
  `Agent` tool** (so they cannot spawn agents — the hierarchy is flat). The only execution path an
  agent has is "call a capability," which the orchestrator runs in a resource-limited container.
- **Deny-by-default approval gate.** Every tool call passes through `can_use_tool`. Anything that is
  not an allow-listed capability is denied; anything needing a credential, payment, account, or
  irreversible external action is routed to a **blocking interactive prompt**.
- **Containerized brain.** The orchestrator runs in its own container and reaches the host Docker
  daemon only through a whitelisted `docker-socket-proxy`, creating sibling sandbox containers.
- **Resource accounting.** Every container has explicit CPU/memory/pids limits, a network policy
  (default `none`), a TTL, and labels; a ledger records each one and flags leaks.

## Free-first + auth

- Prefer free, keyless APIs/libraries/models at every step. A step with no free path stops at the
  approval gate.
- Auth is **OAuth only**: `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`. `ANTHROPIC_API_KEY`
  must be unset (it would override the token) — the brain refuses to start otherwise.

## Prerequisites

1. **Docker** available in this environment (`docker run --rm hello-world` works).
2. `claude setup-token` → put `CLAUDE_CODE_OAUTH_TOKEN` in `.env` (see `.env.example`).

## Run (once prerequisites are met)

```bash
# Phase 0 acceptance (locked-down sandbox round-trip):
pytest tests/test_sandbox_smoke.py

# Milestone 1 (build + verify + reuse a web-search capability):
python -m brain.main "What are the latest developments in <topic>?"
pytest tests/test_m1_web_search.py
```
