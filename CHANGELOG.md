# Changelog

All notable changes to The Project Brain are recorded here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versioning is
[Semantic Versioning](https://semver.org/) — `0.x` while the core algorithm
and behavior are still expected to change between releases.

## [0.5.1] — 2026-07-11

Corrections from the v0.5.0 rerun of the remote-source test (the loop worked:
gap → replan → review gate → honest UNVERIFIED; these fix what the working
loop exposed).

### Fixed
- **Remote observers can no longer skip the staleness gate**: any capability
  that observes (files or network) without a declared output role is stale by
  construction; legacy descriptions are normalized on revalidation (URLs,
  output policy) so residue stops anchoring plans.
- **Extension retargets the review**: the extension's terminal stage becomes
  the artifact-of-record — reviewed and repairable — instead of repair
  rerunning the superseded original stage.
- **Strategy adjudication on replan**: the extension planner must compare
  materially different methods, choose by epistemic fit before cost, justify
  proxies, and emit a traced `strategy_rationale`; mechanism-bound success
  criteria are recompilable (and initial planning is barred from writing
  them).
- **Empty evidence plane bars approval on any high-risk run**, not only
  attached-file runs; the criteria dimension distinguishes
  assessed-not-approved from not-assessed.
- **One authoritative state**: the supervisor reads the review verdicts and
  cannot assert verified/approved against them; inform_user is a nonblocking
  notice; failed observation ≠ observed absence (agents and supervisor).
- **Pure deliverable channel**: `outputs/DELIVERABLE.md` holds the deliverable
  with no engine banner/note/appendix.

## [0.5.0] — 2026-07-11

The adaptive-control release, from the first non-document consumer test (a
remote-source summary). The exposed failure class: the system was open-loop at
the knowledge level — a familiar mechanism substituted for the method the
objective required, its insufficient yield changed nothing, the gap was filled
from remembered narration, and the result shipped as "grounded and
criteria-consistent" with nothing ever assessed.

### Added
- **The loop re-enters on unmet observation needs**: agents declare that the
  objective depends on something their tools cannot observe
  (`report_evidence_gap`); the orchestrator re-enters the core loop at
  "identify the requirements" — the planner extends the running plan for the
  residual need (new stages, new capabilities where none fit), extension
  stages run before any pending review and feed its evidence. Bounded by
  `REPLANS_PER_RUN`.
- **Evidentiary yield ≠ operational health**: observers report `coverage` and
  `unresolved_needs`; partial yield surfaces as an engine note steering the
  agent to the gap signal instead of letting exit-0 read as knowledge.
- **Remote sources are first-class evidence**: remote locators in the
  objective gate grounding review exactly like attached files; observers
  report `source_refs` and persist raw retrieved payloads so review and
  `locate_evidence` reach them; provenance says "observed remote source(s)"
  instead of "from arguments only".
- **Blocker severity**: a fatal blocker is authoritative run state — it can
  prevent SUCCEEDED.

### Fixed
- **Classification honesty**: grounding and criteria dimensions distinguish
  approved / not-required / not-assessed; the success reason states exactly
  what was and was not verified; the supervisor consults on single-stage runs
  too; the deliverable-files appendix is labeled engine audit.
- **Memory laundering**: injected prior-run answers are labeled
  narration-not-evidence (hypothesis; re-establish or deliver as unverified).
- **Consolidation residue**: promotion strips remote locators and
  output-shape policy from capability descriptions.

### Deferred (tracked as issues)
Decision-profile model routing, explicit competing-hypothesis state, abstract
strategy memory, typed multimodal observation flow, full deliverable/audit
envelope separation.

## [0.4.2] — 2026-07-10

Evidence-surface resilience: a mislabeled extractor can no longer silence the
evidence layer (observation-role capabilities preferred, file-consuming
fallback; validators/artifact producers always excluded).

## [0.4.1] — 2026-07-10

### Added
- **Deterministic claim lookup for reviewers** (`locate_evidence`): an
  engine-side tool that searches the run's original evidence (user inputs +
  observation-role extractions, never artifacts) for an exact number/phrase
  and returns located context or an authoritative NOT FOUND. In the v0.4.0
  validation run the reviewer settled "is 90.2% in the source?" by asking the
  producing stage — a tool-less follow-up answering from prose memory — which
  wrongly testified the verbatim source figure was "possibly hallucinated",
  and a TRUE claim was removed. Disputed claims are now adjudicated by direct
  lookup; upstream self-testimony is explicitly barred.

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
