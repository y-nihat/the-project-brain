# Changelog

All notable changes to The Project Brain are recorded here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versioning is
[Semantic Versioning](https://semver.org/) — `0.x` while the core algorithm
and behavior are still expected to change between releases.

## [0.4.0] — 2026-07-10

Grounding and attribution corrections from the v0.3.2 consumer review. The
run could construct a good artifact from bad epistemic input, then reject it
for the wrong mechanical reason — this release fixes both directions.

### Fixed
- **Evidence packet never empty**: budget-aware assembly (headers counted,
  oversize extraction contributes a labeled partial chunk). Previously a first
  file larger than the budget produced an EMPTY packet, so review ran without
  ground truth exactly when the evidence was substantial.
- **Detector confidence**: "definite" truncation now requires structural
  evidence of a cut; unpunctuated lowercase captions ("How Claude works") are
  at most a review-level suspicion — they no longer trigger rewrites,
  a false `artifact=defective` dimension, or quarantine.
- **Repair honesty**: the repair prompt embeds the artifact's whole content or
  skips honestly (no silently clipped "CURRENT content"); an attempt that
  leaves the defect signature unchanged stops the loop and marks the detector
  suspect instead of re-running the same rewrite.
- **Registry/manifest agreement**: quarantine and validation stamps are
  mirrored into the human-facing manifest.

### Added
- **Typed output roles**: capabilities declare `emits` (observation | derived |
  artifact | validation); ORIGINAL SOURCE EVIDENCE is routed by this
  declaration only — consuming a file is not an epistemic role. Undeclared
  output defaults to `derived` (fail-closed for evidence).
- **Contract-fingerprint validation epoch**: validation stamps carry
  ENGINE_VERSION plus a fingerprint of the authoring contract + probe suite,
  so a contract change invalidates promoted capabilities without a release
  bump; a file-consuming capability with no declared output role is stale by
  construction and gets re-authored.
- **Causal quarantine**: a capability is blamed for a delivery defect only if
  the defective file lives in its own output directory, it was not observed
  persisting supplied content verbatim (byte comparison at the runtime
  boundary), and repair did not stall on an unchanged signature.
- **Review verdict record**: `outputs/<review>.md` is written from the
  reviewer's per-round verdicts, never the revised draft's self-description;
  reviewers see artifact head AND tail; an empty evidence packet bars
  approval; unknown load-bearing source facts route to `needs-evidence`.

## [0.2.0 – 0.3.2] — 2026-07-09/10

Tagged incrementally during consumer test cycles; see the annotated tags for
details (`git tag -n9`): procedural-memory immune system, observation-based
provenance, perception-first extraction, mechanical artifact repair,
evidence-chain review, verbatim persistence contract, contract-based reader
discovery.

## [0.1.1] — 2026-07-09

### Epistemic-architecture hardening (from the 2026-07-08 MVP review)
- **No fabricated evidence** (#1): generated capabilities must fail legibly when
  input preconditions are unmet; a no-input probe blocks promotion of any
  input-declaring tool that invents substantive content from nothing.
- **Provenance** (#2): every tool result is labeled with the input it derived
  from (or "no input read"); agents are barred from folding derived/fixture/
  inferred material into a synthesis of user-provided evidence.
- **Objective-derived success criteria** (#3): the planner compiles checkable
  criteria from each request, carried as first-class plan data.
- **Metacognitive review** (#4): reviewers and the supervisor judge the actually
  produced artifact (observed file heads) against the criteria, not the agent's
  self-description.
- **Risk-scaled review** (#5): deeper transformation chains warrant a review stage.
- **Safe consolidation** (#6): the fabrication probe gates promotion.
- **Telemetry integrity** (#7): real model id, prompt-cache token accounting,
  correct stage rendering, honest watchdog wording.
- **Content boundaries** (#8): the developer contract requires contextual
  escaping of untrusted content interpolated into structured output.
- **Fresh-clone fix**: launch scripts pass `--env-file .env` so a clean clone runs.

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
