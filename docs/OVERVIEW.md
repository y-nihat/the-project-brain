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
DC="docker compose -f docker/docker-compose.yml --env-file .env"
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
| **Each agent's full output** | `data/runs/<run_id>/outputs/<agent>.md` (+ `ANSWER.md` = final answer) |
| **Each agent's exact system prompt** | `data/runs/<run_id>/agents/<agent>.prompt.md` |
| Files produced by tools (incl. nested project trees) | `data/runs/<run_id>/workspace/` |

**Continuity:** runs are not amnesiac. Before planning, the brain assembles relevant prior-run
context (objectives, outcomes, produced files, capabilities created) and hands it to the planner
and agents; the most relevant prior run's workspace files are seeded under `workspace/prior/` so
follow-up tasks can build on earlier deliverables. (Semantic retrieval and a 24/7 service mode are
future milestones on this foundation.)

Everything generated lives under `data/` (an **output**, git-ignored) — kept separate from the
engine source in `brain/`. A run never writes to tracked files.

## Interactive session (talk to the brain in a terminal)

A Claude-Code-style REPL: type an objective, watch the brain work **live** (plan → agents → tool
authoring → verification → answer), and every session is recorded under `data/` (and auto-exported
to Phoenix if it's running). This is also where the approval gate can actually stop and ask you
(e.g. for a credential), which non-interactive runs can't.

```sh
./scripts/brain-chat.sh            # brain only  (add --phoenix for the viewer)
# then, at the prompt:
brain> Build and test a small CSV-sorting tool
brain> /runs        # recent runs
brain> /show last   # a run's timeline
brain> /caps        # the capability library
brain> /quit        # (Ctrl-D also) — the script tears the stack down on exit
```

Under the hood both this REPL and the one-shot `python -m brain.main "<objective>"` run through the
same `BrainSession`, so behavior is identical — the REPL just adds the live stream and reuses
connections across turns (so follow-up objectives build on earlier ones via continuity).

## Interactive trace viewing (Arize Phoenix)

The SQLite trace store under `data/` is the source of truth; any recorded run can be pushed to a
local [Phoenix](https://github.com/Arize-ai/phoenix) viewer as OpenTelemetry traces (waterfall
tree: who called whom, when, how long, tokens/cost, errors).

```sh
DC="docker compose -f docker/docker-compose.yml --env-file .env"
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

## How others will use it (M2 — partly built)

- **Built:** the project is pip-installable and ships a host-side `brain` command:
  `pip install .` then `brain "<objective>"` (still needs Docker + the OAuth token
  at run time). `python -m brain.observability stats | backfill` and
  `python -m brain.evals score-corpus` are available too.
- **Not built:** per-project `.brain/` state, and the `brain init` / `brain show`
  verbs you point at an arbitrary folder.
- Per-project state in a `.brain/` dir (like `.git`); shared capabilities and
  agent/team profiles in a global `~/.brain/` — still planned.
- The supported path today remains **docker compose from this repo** as above; the
  installed package runs the same engine but does not yet manage per-project state.

## Status & roadmap

- **Done (through engine 0.28.0):** sandbox + safety substrate; staged
  workflows (single/map/reduce/review) with runtime fan-out; capability
  authoring→verify→promote with revalidation/quarantine and atomic promotion;
  evidence-grounded review; the lineage ledger + deterministic approval gates;
  adaptive control (evidence-gap extension, method-failure recovery,
  brain-decided persistence, research delegation); multimodal perception
  (agents read retained images natively); eval harness; observability.
  `CHANGELOG.md` is the authoritative release record.
- **Capabilities:** `web_search` ships as a bootstrap recipe; everything else
  is authored at runtime per objective and promoted into `data/capabilities/`.
- **Next:** the 1.0 line (see `docs/ARCHITECTURE.md` §11 for the open gaps) →
  M2 package + `brain` CLI → workspace-writing implementer → multi-agent teams.

See also: `docs/ARCHITECTURE.md` (how the core algorithm thinks — diagrams of
the loop, planes, and gates), `README.md`, and `CONTRIBUTING.md` (git workflow).
