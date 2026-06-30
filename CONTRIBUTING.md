# Contributing & Git workflow

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
