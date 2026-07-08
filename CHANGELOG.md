# Changelog

All notable changes to The Project Brain are recorded here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versioning is
[Semantic Versioning](https://semver.org/) — `0.x` while the core algorithm
and behavior are still expected to change between releases.

## [Unreleased]

## [0.1.0] — 2026-07-08

First tagged release. The core loop is built and verified end-to-end:
objective → plan → resolve reuse-vs-create → build/verify/repair capabilities
in Docker sandboxes → execute a staged multi-agent team → deliver, with a
human approval gate for anything that needs a credential or isn't free.

### Added
- **Engine core**: planner (LLM-designed staged workflows: single/map/reduce/
  review), orchestrator (runtime fan-out, bounded parallelism, dependency
  order), capability registry + resolver (reuse before create), sandboxed
  tool authoring with generic verification and self-repair.
- **Safety substrate**: full Docker isolation via a whitelisted socket-proxy
  (brain never touches the host socket directly), hard resource ceilings
  (memory/cpu/timeout, no swap), an interactive approval gate for anything
  needing a credential/payment/account, deny-by-default agent sandboxing.
- **Resilience**: session watchdog (stall + total budget) with partial-output
  salvage, per-capability health monitoring and runtime repair, best-effort
  delivery when a stage fails, failed map-shard retry.
- **Team behavior**: a mediated `ask_upstream`/`report_blocker` channel
  between agents, an evidence-grounded review stage with a `needs-evidence`
  courier loop, model tiers (strong/fast) per engine role.
- **Executive supervision**: a bounded, fail-open judgment layer consulted
  before user interactions and after stage completions — equips the user
  before asking, can send a stage back once against the objective's explicit
  demands.
- **Credential handling**: interactive secret collection (masked receipt,
  visible-typing escape), engine-side injection, author-provided retrieval
  instructions shown before the prompt, session-scoped (never persisted).
- **User-provided files**: `@file` mentions and `/attach`, delivered into the
  run workspace; the brain plans and authors whatever format-handling tools
  the files require.
- **Observability**: SQLite-backed execution history, live terminal
  streaming, Arize Phoenix trace export, a scenario-based eval harness
  (`brain/evals`) with scored, diffable scorecards.
- **Security**: a redaction boundary at the trace layer, a pre-commit secret
  guard, the brain container running as the host user (not root).

### Known limitations (tracked, not yet addressed)
- Redaction does not yet cover every disk-writing path (sandbox stderr,
  failed-verify artifacts, the blackboard, run-folder outputs) — a hardening
  slice is planned before this is relied on with real credentials.
- No persistent/long-running services (e.g. a live chat bot) — only
  request/response tool calls.
- Secrets are session-scoped only; no cross-session credential storage.
- Capability resolution is exact-key match (no semantic/fuzzy reuse).
- No workspace-mount mode yet — the brain only operates on files it is
  explicitly given, not an arbitrary project directory.
