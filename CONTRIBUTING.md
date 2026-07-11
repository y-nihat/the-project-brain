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

## Logical grouping — one concern per commit

Group related changes; keep unrelated changes in separate commits. When a working
tree mixes concerns, stage them in groups (`git add -p` / per path) and commit
each separately. The log should read like a story: a future reader can follow
*what changed and why*, one idea at a time.

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
