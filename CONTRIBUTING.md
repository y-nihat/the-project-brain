# Contributing & Git workflow

## Architectural invariant — the core loop (read this first)

The brain's core loop is fixed and every change must preserve it:

> **analyze the incoming task → break it down into steps → identify the
> requirements → generate the agents and tools needed to fulfill them →
> execute.**

Concretely: the planner derives the workflow and success criteria from the
objective (never from a template); each stage declares what it needs; the
resolver reuses or the brain authors the capabilities; agents are synthesized
per stage; execution runs under supervision and review.

The loop may RE-ENTER itself mid-run: when execution reveals an unmet
observation need (an agent reports that the objective depends on something its
tools cannot establish), the engine re-enters at "identify the requirements"
for the residual need — the planner extends the plan, the brain acquires the
missing methods, execution continues. Iteration is part of the loop;
replacing it is not.

Two rules follow:

1. **No scenario-specific changes.** An engine change must be statable as a
   sentence with no task, topic, or tool NAME in it. Tools are identified by
   their declared contracts (e.g. `accepts`, `requires_credential`), never by
   name. A fix derived from a failing task must address the general failure
   class, not the task.
2. **No fixed pipelines.** Never hardcode a stage sequence, a predefined agent
   role, or a task-shaped branch into the engine. Safety/quality machinery
   (validation, provenance, review, repair) wraps the loop; it must never
   replace or bypass the planner's per-objective design.

A lightweight, industry-standard git workflow so changes stay easy to track.
Until we publish to GitHub we manage everything locally.

## Commit messages — Conventional Commits

Format: `<type>(<scope>): <summary>`

- **Imperative mood**, summary ≤ 72 chars ("add X", not "added X").
- A body explaining **why** (the motivation/problem), wrapped ~72 columns.
- Trailers for breaking changes / attribution.

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`,
`ci`, `chore`, `revert`.

Scopes (this codebase): `sandbox`, `registry`, `ledger`, `approval`, `planner`,
`resolver`, `factory`, `acquisition`, `orchestrator`, `capabilities`, `memory`,
`observability`, `config`, `docker`, `tests`, `deps`.

Examples:

- `feat(observability): add execution-history tracer and trace viewer`
- `fix(sandbox): reap container when wait() times out`
- `test(acquisition): cover the no-free-path approval gate`
- `chore(deps): pin docker-py to >=7`

A `commit-msg` hook (`.githooks/commit-msg`) validates this format.

## Hooks — what runs when

| Hook | Guards |
| --- | --- |
| `pre-commit` | refuses staged changes containing credential-shaped strings |
| `commit-msg` | validates Conventional Commits format |
| `pre-push` | runs the offline test suite; a red suite blocks the push |

All three are enabled per clone by `./scripts/setup-git.sh` (it sets
`core.hooksPath`). `git push --no-verify` is the escape hatch when you know
why you need it.

There is deliberately **no hosted CI**. This is a private, single-author repo
with no deploy target, and 11 of the tests need a Docker daemon plus registry
and PyPI egress — a hosted runner would need real setup to cover a minority of
the suite. The other ~430 tests are pure Python, skip their Docker-gated peers
cleanly, and finish in under ten seconds, which is affordable on every push.
Revisit when a second committer joins or the repo goes public.

## Logical grouping — one concern per commit

Group related changes; keep unrelated changes in separate commits. When a working
tree mixes concerns, stage them in groups (`git add -p` / per path) and commit
each separately. The log should read like a story: a future reader can follow
*what changed and why*, one idea at a time.

## Where a test lives

**A test lives in the file named after what it CONSTRAINS — never after when it
was written.** If it constrains a module, it goes in that module's file
(`brain/workflow.py` → `tests/test_workflow.py`). If it constrains a
cross-cutting property, it goes in that property's file (`test_epistemics.py`,
`test_leakproof.py`, `test_secrets.py`, `test_conversation.py`).

Do **not** open a new `test_v<version>.py`. Release-named files accumulated one
per version and turned the suite into a journal, which cost exactly what a
journal costs: a test is read when something breaks, and a version name answers
none of "what covers this?"; nothing tells the author a behaviour is already
covered, so each release re-tests it — every duplicate pair found in the 2026-07
audit had the version file holding the *weaker* copy; and because such a file is
only ever appended to, dead contracts pile up in it. The release is recorded in
the commit and the CHANGELOG, which is where history belongs.

The existing `test_v014.py` … `test_v028.py` are migrated by attrition: when you
touch a test in one for any reason, move it to its subject's file then. Do not
mass-move them — dropping a test in a bulk shuffle costs more than the tidiness
is worth. Take the docstring with it: this suite's docstrings cite the observed
failure each test exists for, and that is the part worth preserving.

One genuine exception: a test whose subject really *is* a version boundary —
schema migration, backward compatibility, reading an old format with a new
engine. Name it for that (migration, compatibility), not for the release.

## Branching

- **Now (local only):** trunk-based — commit small, coherent changes directly to
  `main`. A plan-mode-approved slice lands as one or a few logical commits.
- **After we create the GitHub repo:** short-lived feature branches + pull
  requests for review before merging to `main`.

## History

We do **not** rewrite shared history. Local-only cleanup (squashing a messy WIP
series *before* it is shared) is fine; once pushed, history is immutable.

## One-time setup (per clone)

```sh
./scripts/setup-git.sh   # sets commit.template + core.hooksPath
```
