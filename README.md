# The Project Brain

A self-organizing agent **brain** built on the Claude Agent SDK. Give it an objective; it plans the
work, decides which agents/capabilities already exist vs. must be built, builds missing capabilities
**inside Docker sandboxes**, verifies they actually work, and promotes the reusable ones into a
growing capability library — under strict free-first, approval-gate, and full-isolation constraints.

Its distinguishing property is not autonomy but **honesty**: a deliverable's load-bearing claims must
trace to files a tool actually retained, and a run that cannot prove its work says so
(`unverified` / `blocked` / `failed`) instead of narrating success.

> **Status:** engine **v0.31.0**, pre-1.0, actively developed by one author. The safety substrate,
> staged workflows, capability authoring/verification/promotion, the evidence-and-review plane,
> adaptive recovery, standing goals, the eval harness, and observability are built and covered by
> 523 tests. `CHANGELOG.md` is the authoritative release record.
> See **[docs/OVERVIEW.md](docs/OVERVIEW.md)** for the stack, how to run it, and where logs live.

## Try it in about a minute

```sh
git clone <this repo> && cd the-project-brain
claude setup-token                      # then put CLAUDE_CODE_OAUTH_TOKEN in .env
cp .env.example .env && $EDITOR .env

DC="docker compose -f docker/docker-compose.yml --env-file .env"
$DC up -d --build
$DC exec brain python -m brain.main "In one sentence, what is idempotency? No tools needed."
```

Nothing about that objective has to be *observed*, so the analyze step answers it directly: about
**10 seconds, a couple of cents, zero containers**, and the answer says in its own text that it
traces to nothing. That confirms auth and the answer path — not the sandbox. For the rest of the
stack, run something that must observe:

```sh
$DC exec brain python -m brain.main "Use web search to find the current stable Python version, and cite the source URL."
```

That one plans, acquires a tool, runs a container, and cites what it saw — about a minute and
well under a dollar. Then try attaching a file with `@`.

For an interactive session with follow-up turns and live progress, use `./scripts/brain-chat.sh`.

## Prerequisites

1. **Docker** available (`docker run --rm hello-world` works). Everything agent-authored runs in a
   container; nothing runs on your host.
2. `claude setup-token` → put `CLAUDE_CODE_OAUTH_TOKEN` in `.env` (see `.env.example`).
3. **`ANTHROPIC_API_KEY` must be unset.** It silently overrides the OAuth token, so the brain
   refuses to start when it is present.
4. If your host user is not uid/gid 1000, set `BRAIN_UID`/`BRAIN_GID` in `.env` so files the brain
   writes under `data/` belong to you.

Runs cost real money. Over 108 recorded runs: **$0.01 to $10.74**, median **$1.16**, p90 $4.83. A
turn that needs nothing observed is answered directly and costs cents. Both entry points print what
a turn spent — and, while it is still running, what it has spent so far — and
`python -m brain.observability stats` summarizes spend by outcome.

## Installing as a package

```sh
pip install .          # provides the `brain` command
brain "<objective>"    # still requires Docker + CLAUDE_CODE_OAUTH_TOKEN at run time
```

Installability is not yet the full M2 milestone: per-project `.brain/` state and the
`brain init` / `brain show` verbs are not built. See `docs/OVERVIEW.md`.

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

`SECURITY.md` states both the guarantees and — deliberately — the non-guarantees.

## Free-first + auth

- Prefer free, keyless APIs/libraries/models at every step. A step with no free path stops at the
  approval gate.
- Auth is **OAuth only**: `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`.

## Development

```sh
./scripts/setup-git.sh   # commit template + hooks (secret guard, message format, test gate)

# Run the suite INSIDE the container — that is the environment it targets.
DC="docker compose -f docker/docker-compose.yml --env-file .env"
$DC exec brain python -m pytest tests/            # 523 tests
```

On the host the suite still runs; the 11 sandbox tests skip and say why. They gate
on whether this machine can actually **build** a sandbox image, not on whether a
daemon answers — the two come apart, and asking the wrong question turned a
healthy push into seven red tests.

`CONTRIBUTING.md` has the git workflow and — read first — the architectural invariant every change
must preserve. `docs/RATIONALE.md` maps each subsystem to the release entries that explain why it
exists.
