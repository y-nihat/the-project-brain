# The Project Brain

A self-organizing agent **brain** built on the Claude Agent SDK. Give it an objective; it plans the
work, decides which agents/capabilities already exist vs. must be built, builds missing capabilities
**inside Docker sandboxes**, verifies they actually work, and promotes the reusable ones into a
growing capability library — under strict free-first, approval-gate, and full-isolation constraints.

Its distinguishing property is not autonomy but **honesty**: a deliverable's load-bearing claims must
trace to files a tool actually retained, and a run that cannot prove its work says so
(`unverified` / `blocked` / `failed`) instead of narrating success.

> **Status:** engine **v0.32.0**, pre-1.0, actively developed by one author. The safety substrate,
> staged workflows, capability authoring/verification/promotion, the evidence-and-review plane,
> adaptive recovery, standing goals, the eval harness, and observability are built and covered by
> 542 tests. `CHANGELOG.md` is the authoritative release record.
> See **[docs/OVERVIEW.md](docs/OVERVIEW.md)** for the stack, how to run it, and where logs live.

## Try it

```sh
git clone https://github.com/y-nihat/the-project-brain.git && cd the-project-brain
cp .env.example .env && chmod 600 .env
mkdir -p data                           # Docker would otherwise create it as root
claude setup-token                      # paste the token into .env as CLAUDE_CODE_OAUTH_TOKEN

DC="docker compose -f docker/docker-compose.yml --env-file .env"
$DC up -d --build                       # first build ~2 min (image pull + CLI install); seconds after
$DC exec brain python -m brain.main "In one sentence, what is idempotency? No tools needed."
```

Nothing about that objective has to be *observed*, so the analyze step answers it directly. Measured
on a clean install of this exact tree — fresh clone, empty `data/`, no prior configuration:
**13 seconds, $0.01, zero containers**, and the answer says in its own text that it traces to
nothing. That confirms auth and the answer path — not the sandbox. For the rest of the stack, run
something that must observe:

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

Runs cost real money, on **your** subscription. One derivation, from this repository's own recorded
history via `python -m brain.observability stats`: **121 runs, $220.27 recorded spend**. Of those,
114 carry accounting — **$0.01 to $10.74 per run, median $1.07, p90 $4.79** (nearest-rank, over runs
with a recorded cost) — and 7 aborted runs still have none, so they are missing from every number
here; `python -m brain.observability backfill` recovers them. A turn that needs nothing observed is
answered directly and costs cents. Both entry points print what a turn spent, and what it has spent
so far while it is still running.

**There is no spend ceiling yet.** The limits are counts (agent turns, sessions per run, repair
attempts), not dollars. Watch the live counter on your first few runs.

## Installing as a package — not supported yet

`pip install .` builds and exposes a `brain` command, but **use compose instead.** The installed
path is incomplete in ways that matter: the orchestrator runs on your host rather than in its own
container, so the containment described below applies only to the sandboxes and not to the brain
itself; state (the trace DB, run folders, the capability library, the `.env` it reads) lands beside
the installed package in `site-packages`; and per-project `.brain/` state with the `brain init` /
`brain show` verbs is not built. See `docs/OVERVIEW.md`.

## Safety model (enforced structurally, not by trusting the model)

- **Nothing agent-generated runs on the host.** Agents are LLM sessions the orchestrator drives;
  their only way to act is a tool call. Agents get **no host `Bash`, no host file-write, and no
  `Agent` tool** (so they cannot spawn agents — the hierarchy is flat). The only execution path an
  agent has is "call a capability," which the orchestrator runs in a resource-limited container.
- **Deny-by-default tool gate.** Every tool call passes through `can_use_tool`; anything that is not
  an allow-listed capability is denied.
- **Blocking prompt for credentials.** A step that needs a credential the session does not hold
  stops at an interactive prompt, and the secret is collected there rather than through the model.
  An objective that itself looks like it contains a credential is intercepted before it is recorded.
- **Containerized brain, under compose.** The orchestrator runs in its own container and reaches the
  host Docker daemon only through a whitelisted `docker-socket-proxy`, creating sibling sandbox
  containers. As `docker/docker-compose.yml` says in its own header: controlling container creation
  is still powerful — the proxy shrinks the blast radius, it does not make the brain harmless. This
  bullet describes the compose path only; see "Installing as a package" above.
- **Resource accounting.** Every container has explicit CPU/memory/pids limits, a network policy
  (default `none`), a TTL, and labels; a ledger records each one and flags leaks.

**What is NOT gated per action.** There is no approval prompt at the moment an authored tool acts.
A capability the brain built can be granted network egress (the model chooses `network: bridge` when
it judges the tool needs it) and can write files into its run workspace, and it does both without
asking. The gate above is about *credentials* and *which tools may be called*, not about each
outward action. Treat an objective you did not write, or content the brain fetches, as untrusted
input to a system that writes and runs code — prompt injection is in scope and undefended.

`SECURITY.md` is where to report a vulnerability; `docs/SECURITY_MODEL.md` states both the
guarantees and — deliberately — the non-guarantees.

## Free-first + auth

- Prefer free, keyless APIs/libraries/models at every step. A step with no free path stops at the
  approval gate.
- Auth is **OAuth only**: `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`.

Every operator brings their own credential. `claude setup-token` is Anthropic's documented mechanism
for scripts and CI, the token requires your own Pro/Max/Team/Enterprise plan, and Agent SDK usage
draws on that plan's monthly Agent SDK credit. It can only make model requests. Keep it in your own
`.env` (mode 600, git-ignored): credentials are per-account and are never shared, pooled, or
committed. No token ships with this project and none ever should.

## Non-goals

- **Not a hosted service.** One shared instance running many people's work on one credential is
  outside both the design and the credential's terms. Run your own.
- **Not a Claude Code replacement.** This drives the Agent SDK for autonomous, staged runs; for
  interactive coding, use Claude Code.
- **Not multi-provider yet.** OAuth only. Local models and other providers are on the roadmap, not
  in the engine.
- **Not hardened against a malicious operator.** Anyone with your user account or Docker daemon can
  read `.env` and container environments; that is out of scope, and stated as such.
- **Not a spend-controlled system yet.** See the cost note above.

## Support

Built and maintained by one author, in the open, pre-1.0. Issues and discussions are welcome and
answered when there is time; there is no SLA. Security reports go through `SECURITY.md`, which is
the one channel with a stated response commitment. Interfaces may change before 1.0 — pin a tag if
you need stability.

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

## License

**AGPL-3.0** — see [`LICENSE`](LICENSE).

Run it on your own machine, the documented way, and the license constrains nothing: use it, change
it, do what you like. Section 13 governs one case — **offering this software to others over a
network as a service** — and requires that you publish your modified source under the same license.
If that does not fit your product, a commercial license is available; open an issue titled
"commercial licensing".

Contributions are accepted under a Contributor License Agreement ([`CLA.md`](CLA.md)) — sign off with
`git commit -s`. It is what keeps the commercial option open; in exchange, every accepted
contribution stays available under the AGPL.

[`NOTICE`](NOTICE) records the third-party terms this license does not cover: most importantly, the
container image installs Anthropic's proprietary Claude Code CLI, so do not publish a prebuilt image
of this project to any registry. Build it locally, as above.
