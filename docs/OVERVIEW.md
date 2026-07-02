# The Project Brain — overview, stack, and how to run it

A practical map of **what exists today**, **what it runs on**, **how to execute
it**, and **where the logs are**. Living document — updated as the system matures.

## What it is

An orchestrator ("brain") that takes an objective, plans the work, decides which
capabilities exist vs. must be built, builds missing ones **inside Docker
sandboxes**, verifies them, and promotes the reusable ones into a growing
library. Free-first, with a human approval gate and full container isolation.

## The stack (what it's built on)

- **Claude Agent SDK (Python)** — the package `claude-agent-sdk`. There is **no
  "Tende CLI"**: the SDK drives the **Claude Code CLI** (the `claude` binary) as a
  subprocess, which in turn talks to the Claude models.
- **Auth:** OAuth via `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`).
  `ANTHROPIC_API_KEY` must be unset (the brain refuses to start otherwise).
- **Isolation:** Docker. Agent-built code/tools run *only* in sandbox containers
  with strict CPU/memory/network limits; the brain reaches the Docker daemon
  through a whitelisted `docker-socket-proxy`.
- **Storage:** SQLite (`data/brain.db`) + a capability store + per-run trace files.

```
host (WSL2 + Docker Desktop)
└─ docker compose
   ├─ docker-socket-proxy        (whitelisted access to the Docker daemon)
   └─ brain  (container)         our Python + Claude Agent SDK + claude CLI
        │  plan → resolve → acquire → execute   (+ tracer records everything)
        └─ creates short-lived SANDBOX containers (capability build/verify/run)
```

## Environment

- WSL2 (Ubuntu) + Docker Engine/Desktop + Compose.
- The brain runs **inside a container** (compose service `brain`) next to
  `docker-socket-proxy`.
- `.env` at the repo root holds `CLAUDE_CODE_OAUTH_TOKEN`.

## How to run it **today**

```sh
DC="docker compose -f docker/docker-compose.yml"
$DC up -d --build        # first time builds the image; afterwards just: $DC up -d
$DC exec brain python -m brain.main "<your objective>"     # run an objective
$DC exec brain python -m pytest                            # tests
$DC exec brain python -m brain.observability list          # list runs
$DC exec brain python -m brain.observability show --last    # full run timeline
```

> If Docker Desktop restarts, re-run `$DC up -d` (the brain container has no
> restart policy, so it won't auto-resurrect).

## Where the logs / results are saved

All under `data/` in the repo (bind-mounted from the container, so you can open
them directly in your editor). `data/` is git-ignored (runtime state).

| What | Path |
| --- | --- |
| Human-readable run timeline | `data/runs/<run_id>/trace.md` |
| Live/tailable event stream | `data/runs/<run_id>/trace.jsonl` |
| Queryable store (runs, events, ledger, registry, blackboard) | `data/brain.db` |
| Promoted reusable tools (manifest + build context) | `data/capabilities/<name>/` |
| Tools a run created (agent attempt + promoted scripts) | `data/runs/<run_id>/artifacts/<capability>/` |

Everything generated lives under `data/` (an **output**, git-ignored) — kept separate from the
engine source in `brain/`. A run never writes to tracked files.

## Interactive trace viewing (Arize Phoenix)

The SQLite trace store under `data/` is the source of truth; any recorded run can be pushed to a
local [Phoenix](https://github.com/Arize-ai/phoenix) viewer as OpenTelemetry traces (waterfall
tree: who called whom, when, how long, tokens/cost, errors).

```sh
DC="docker compose -f docker/docker-compose.yml"
$DC --profile observability up -d phoenix          # start the viewer (opt-in profile)
$DC exec brain python -m brain.observability export --last   # or <run_id>, or --all
# browse http://localhost:6006 — then stop everything:
$DC --profile observability down
```

Phoenix's data persists under `data/phoenix/`, so the viewer can be stopped and restarted anytime
without losing exported traces. It runs resource-capped (1 GB / 1 CPU) like everything else.

## Resource model (sandboxes are capped, measured, and cleaned up)

- **Hard limits, no swap.** Every sandbox container gets `memory` (default 256 MB, swap disabled so
  the limit is real), `cpus` (0.5), `pids`, and a timeout (120 s).
- **Tools declare needs; the engine clamps.** An authored tool may declare
  `resources: {memory_mb, cpus, timeout_seconds}`; the engine bounds them to ceilings
  (2048 MB / 2 CPU / 300 s — overridable via `SANDBOX_MAX_*` env vars).
- **OOM kills are detected and reported** (`RunResult.oom`, ledger `oom` column, trace span) — and
  fed back to the tool-authoring self-repair loop in plain language, since an OOM kill leaves no
  traceback.
- **Usage is measured**: a best-effort sampler records each container's peak memory
  (`peak_mem_mb` in the ledger and trace) so limits can be tuned from data.
- **Output capture is bounded** (`SANDBOX_MAX_OUTPUT_MB`, default 128 MB) so a runaway tool cannot
  exhaust the orchestrator.
- **The control plane is capped too**: compose sets brain 2 GB / 2 CPU, socket-proxy 128 MB / 0.5.
- **No sprawl**: session containers are reaped; dangling brain-built image layers are pruned on
  shutdown (tagged capability images — the library — are kept).
- **Guaranteed stack teardown**: use `./scripts/run-objective.sh "<objective>"` to run an objective —
  it always tears the compose stack down afterwards, even on failure/Ctrl-C.

## How others will use it (planned — **not built yet**, milestone M2)

- Installable Python package + a host-side **`brain` CLI** you point at any folder:
  `brain init`, `brain build "<objective>"`, `brain show --last`.
- Per-project state in a `.brain/` dir (like `.git`); shared capabilities and
  agent/team profiles in a global `~/.brain/`.
- Until M2 ships, the system is **run from this repo via docker compose** as above
  (not yet `pip install`-able).

## Status & roadmap

- **Done:** Phase 0 (sandbox + safety substrate), Milestone 1 (`web_search`
  capability, sandboxed/verified/reusable), Observability (tracer + viewer),
  Git conventions.
- **Capabilities available:** `web_search` (DuckDuckGo via `ddgs`, no API key).
- **Next:** evaluation harness → M2 package + `brain` CLI → workspace-writing
  implementer (build real projects in a folder) → multi-agent teams.

See also: `README.md`, `CONTRIBUTING.md` (git workflow), and the plan file in
`~/.claude/plans/`.
