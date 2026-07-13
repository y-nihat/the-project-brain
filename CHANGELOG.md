# Changelog

All notable changes to The Project Brain are recorded here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versioning is
[Semantic Versioning](https://semver.org/) — `0.x` while the core algorithm
and behavior are still expected to change between releases.

## [0.11.0] — 2026-07-13

The provable-certificate release (Phase A of the 1.0 roadmap, plus approved
hygiene and core-strengthening items). The exposed failure class: a run derived
its answer from genuine property-preserving transformations of the designated
target, yet its formal ledger held only proxy items and approval passed on mere
non-emptiness — a TRUE success with a HOLLOW certificate, which is the same
structural hole that once let a FALSE success through. Separately, an
extraction failure was indistinguishable from a demonstrated absence.

### Added
- **Lineage flows through transformations**: the runtime records, per tool
  call, the files retained, the locators ACTUALLY accessed, the workspace files
  ACTUALLY consumed, the reported yield/state, the producing role, and a
  content hash per payload. The ledger propagates lineage to a fixpoint:
  `target` (direct access of a designated source, or user input) → `derived`
  (produced by consuming target-lineage inputs — samples of a recording, pages
  of a document, rows of a dump, output of a program under test) — whatever
  role the producing tool self-declared. Consuming only proxy inputs stays
  proxy; unknown provenance stays `retained` and is never proof. Review's
  evidence packet and `locate_evidence` now include target-lineage payloads.
- **Claim-support gating**: on source-derived runs the reviewer must emit a
  typed ```claims table (direct / derived / inferred / negative / proxy /
  unsupported, each citing its evidence file). The engine deterministically
  voids an approval with: no table, any unsupported claim, no direct/derived
  support at all (inference and testimony cannot certify the source), or a
  direct/derived/negative claim whose citation does not resolve to a
  target-lineage ledger file. A NON-EMPTY but proxy-only ledger now voids —
  closing the hollow-certificate hole. `run.dimensions` gains
  `target_evidence`.
- **Standardized observation states**: observation-role tools must report
  `observation_state` — observed_present / observed_absent / not_detected /
  ambiguous / observation_failed — with deterministic contradiction checks
  (present∧none, absent-or-failed∧complete). The contract's hard rule: a
  processing failure must NEVER produce the same output as genuine emptiness,
  and only `observed_absent` can support an absence claim ('negative' claims
  are gated on it; agents are steered to "no X was detected", never "there is
  no X"). The runtime's yield note carries the state and the absence
  discipline.
- **Deeper research**: the research agent now fetches and reads authoritative
  pages (`fetch_page_text`, authored on first use under a clean instance-
  neutral contract) instead of trusting search snippets, marks findings by
  confidence, proposes a verification step for brittle claims, and its brief
  warns against compiling snippet-only claims into expensive retries.
  `RESEARCH_AGENT_MAX_TURNS` is now actually enforced (it was declared and
  unused — a 19-turn live session proved it).
- **Persistence judgment upgraded**: failure-boundary decisions route to the
  STRONG model tier and draw on their own budget (`PERSISTENCE_MAX_CALLS`) so
  routine consultations can never starve them.
- **Semantic criteria decomposition**: the planner compiles criteria per
  requested dimension of understanding (events, mechanism/causes, topic/theme,
  evaluation, chronology) — an accurate event inventory no longer satisfies a
  request to explain subject or meaning — and interpretation must be presented
  as interpretation, distinct from observation.
- **Stage expectations persist** into the traced plan roster;
  `capability.recovered` traces success-after-retries with per-attempt history.

### Fixed
- Retained payloads use unique filenames by contract (repeated observations no
  longer overwrite earlier evidence; the search recipe complies and reports
  `observation_state`); tool self-reports reference bare filenames instead of
  sandbox-local paths; the repo secret-shape scan excludes managed virtual
  environments (a `.venv` false positive, not a leak).

## [0.10.0] — 2026-07-12

The problem-solver release: two approved capabilities that move the system from
"fail honestly" toward "research and persist toward success" — built ON the
honesty core, not against it.

### Added
- **Research delegation channel**: an agent that recognizes it lacks KNOWLEDGE
  (an unfamiliar library/API, an opaque error, a technique or design direction)
  can ask the orchestrator to research it via `request_research` — mediated (the
  agent never searches inline), reusing the proven `ask_upstream`
  request→delegate→return pattern. A bounded research agent (web_search)
  investigates and returns a findings brief; a shared per-run budget
  (`RESEARCH_MAX_REQUESTS`) and a topic cache keep it bounded and traced. The
  channel reaches **developer/repair agents too** — which previously had no
  orchestrator channel at all — so a developer can diagnose a build/runtime
  error through research instead of re-authoring blindly. Honesty guardrail:
  findings are labeled METHOD KNOWLEDGE / testimony and can never become
  source evidence about the designated target (web_search payloads stay
  proxy-tier in the ledger by construction).
- **Brain-decided persistence + structured help request**: at a failure/recovery
  boundary the SUPERVISOR judges — from the attempt history and the
  expectation-vs-observation gap — whether to `keep_trying` (a materially
  different approach remains and attempts are still making progress),
  `stop_and_ask` (approaches exhausted / progress plateaued / the user must
  provide something), or `stop` (no admissible path). This replaces a flat
  counter with a human-like judgment; it can only make the run stop EARLIER,
  and a raised-but-finite `RECOVERIES_PER_RUN` plus the per-run session cap
  remain hard safety rails (fail-open to `keep_trying`). The planner now emits
  a per-stage `expectation` (what a successful observation should yield) that
  feeds the judgment. When it stops for input, the run emits a structured help
  request — how far it got, what it tried, why it stopped, and exactly what it
  needs from you — and classifies `blocked` (needs-you), not a dead end.

## [0.9.1] — 2026-07-12

Bug-fix release from the two final runs of the video task. The frame pipeline
was architecturally correct — 402 frames extracted, a 12-worker perception map,
synthesis, review — but a one-line regression made it observe nothing, and the
run burned its session cap retrying the broken tool ($10.27, blocked). No new
features; correctness and honesty fixes only.

### Fixed
- **`read_file` crashed on every call (the regression that killed multimodal
  perception)**: it passed `kind=` to `Tracer.event`, colliding with that
  method's positional `kind` parameter, so every image read returned "got
  multiple values for argument 'kind'". Renamed the field; the whole handler is
  now guarded so an engine fault fails loud and self-labeled ("do NOT retry …
  report a blocker") instead of becoming a retry sink. **Root cause of the miss:
  the perception path was only ever tested with `NullTracer`, which swallows
  `**kwargs`** — a real-`Tracer` regression test now covers it.
- **An unparseable review verdict no longer auto-approves**: a review whose
  prose lacks the exact `VERDICT:` token defaults to `revise` (was `approved`) —
  a synthesis grounded in zero observed frames had been auto-approved on an
  unparsed verdict.
- **File listings say "showing first 40 of N"** instead of implying only 40
  files exist (a worker concluded the video had 40 frames when 402 were
  extracted).
- **A `blocker_class` must be justified by the current run**, not by a prior
  run's failure, and `resource_ceiling` may not be claimed for acquisition,
  conversion, or perception (only for loading a heavy model/payload at once) —
  a run had proxied a URL by citing a stale OOM from an approach no longer used.

## [0.9.0] — 2026-07-12

The accessible-source release. A file named plainly in the objective (not via
`@`) never reached the run, and when a designated file could not be accessed
the system authored a doomed, file-starved pipeline instead of asking. The deep
reason is isolation, by design: sandboxed tools receive files ONLY through the
engine's `put_archive` into `/work/in/` — there is no host mount — so a path
the engine does not resolve into the workspace can never reach a tool, however
the agent spells it.

### Added
- **Files designated by path are resolved and attached** — not only `@`-mentions.
  `files.resolve_local_sources` detects a file the user named (backticked or
  bare, with a real extension), resolves it against the project, the brain's
  mount, and the sanctioned inbox, rewrites the reference to `input/<name>`, and
  materializes it. URLs, `input/` tokens, and non-file shapes (`e.g.`, `v3.12`)
  are left alone; only files the engine can actually reach are copied
  (isolation-safe).
- **Interactive "accessible file" gate (the credential gate, for files)**: when
  a designated file cannot be accessed, the run ASKS the user for a correct path
  via a new visible-input broker channel (`ApprovalBroker.collect_value` —
  shown on screen, never stored as a secret), bounded by `FILE_ASK_ATTEMPTS`. A
  good answer is materialized and the objective repointed at it; a decline (or
  an automated session) proceeds with the file marked unavailable — explicitly,
  never by silent retry.
- **Honest starvation instead of doomed work**: a file that stays unavailable is
  annotated in the objective ("could NOT be accessed … report a fatal blocker …
  never fabricate"), and the runtime's not-found message now steers an agent to
  a single fatal blocker rather than guessing paths — so a missing required
  file ends the run BLOCKED with a clear ask, not an expensive dead-end pipeline.

## [0.8.0] — 2026-07-12

The autonomous-observation release. Two runs of the video task exposed the gap:
given explicit human-style steps the planner built the correct pipeline
(extract frames → describe → synthesize), proving the capability exists — but
on the generic phrasing it declared "no video-playback tool is available; this
is a concrete blocker" and fell back to web search. It treated tool-absence as
impossibility in a system whose premise is authoring tools, and separately it
tried to BUILD a vision model (which OOM-killed at the 2 GB sandbox ceiling)
when the reasoning model is already multimodal.

### Added
- **Tool-absence is never a blocker (proactive tool creation)**: the planner
  emits an accountable `direct_observation` declaration — `planned` (a stage
  observes the designated source, authoring the tool if needed) or `infeasible`
  with a `blocker_class` from a CLOSED enum (`requires_credential`,
  `resource_ceiling`, `no_free_means`, `physical_access`, `user_authority`).
  "No tool exists" is not a member, so it cannot justify a proxy. A
  source-derived plan that skipped direct observation with no legitimate
  blocker is detected deterministically and re-planned ONCE with a pointed
  corrective (bounded, fail-open) — the system builds the needed capability
  instead of settling for a weaker channel.
- **Observation is decomposed into authorable sub-steps**: acquire the object
  → convert its carrying signal into files → perceive those files. The planner
  names the atomic tools (a downloader, a frame/segment/page extractor) for the
  system to build.
- **Multimodal perception is the agent's own ability**: every stage agent gets
  an image-aware `read_file` tool — images come back as image content blocks —
  so "describe these frames / screenshots / scans / charts" is the agent's
  native ability (like summarizing), NOT a capability. The planner is barred
  from authoring VLM/OCR/caption/vision-model tools (they duplicate the agent's
  sight and blow the sandbox ceiling) and instead authors the cheap tool that
  PRODUCES the files to look at. Generalizes v0.7.0's review-only image viewing
  to all stages; `view_evidence` is subsumed by `read_file`.
- **Inbox files named by path are attached**: a file dropped in the sanctioned
  `data/inbox` and named in the objective by path (backticked or bare) is
  materialized into the run like an `@`-mention — a first-class way to
  designate a source directly, alongside `@`-mentions and remote locators.
  Only files that resolve inside the inbox are touched.

### Deferred (tracked)
Fail-fast when a named local file is neither attached nor in the inbox (today:
proceeds without it); per-frame map-sharding heuristics for long media;
resource-ceiling method failures auto-pivoting to agent-native perception.

## [0.7.0] — 2026-07-12

The trusted-evidence release, from the v0.6.0 rerun of the remote-source test.
The exposed failure class: the run's "evidence plane" existed only as prompt
assembly — a reviewer acknowledged an empty plane, then approved on courier
narration with all 17 deterministic lookups NOT FOUND, and the engine took the
parsed verdict over its own facts. Proxy-first strategy persisted because
proxy-first still got rewarded, and the strategy space never contained the
target's own signals: the system could not perceive non-text observations, so
plans never proposed producing them.

### Added
- **The evidence ledger** (`brain/evidence.py`): deterministic typed state of
  what the run has ACTUALLY retained. The capability runtime records each tool
  call's retention + reported provenance (`evidence.jsonl`); entries are
  tiered `target` (payload from a DESIGNATED source — objective locators or
  attached inputs), `proxy` (payload from elsewhere: testimony ABOUT the
  target), or `retained` (unknown lineage). Agent prose is never in the
  ledger, whatever it calls itself. Review context carries the tier-labeled
  ledger.
- **Deterministic approval gate**: a parsed `approved` cannot override engine
  facts — voided (traced, recorded as `approved-voided`) when a source-derived
  run has an EMPTY ledger, or when a MEASURED count criterion is violated.
  Courier output is labeled by the ledger diff: `NEWLY RETAINED EVIDENCE`
  only when files actually appeared, else `COURIER REPORT (narration, NOT
  evidence)` — the engine itself was the laundering channel before.
- **Escalation past the courier's toolbox**: an unresolved evidence demand on
  a source-derived run with an empty ledger becomes an evidence gap for the
  adaptive loop (the courier can only re-run the tools whose insufficiency
  caused the demand — it can never author new observation); a post-review
  extension re-appends a FRESH review over the new terminal, so new evidence
  is never shipped unreviewed. Bounded by `REPLANS_PER_RUN`.
- **One evidence contract for every observer**: the recipe/bootstrap path now
  runs the same generic verification (yield fields, lineage, retention) as
  authored tools — the most trusted path had the weakest contract. The
  web_search recipe retains raw payloads, reports the index it ACTUALLY
  accessed as `source_refs`, and declares snippet yield `partial` with the
  unfetched pages as unresolved needs.
- **Typed deliverable payload**: deliverable-producing stages emit a fenced
  payload block; `DELIVERABLE.md` is the payload verbatim; review judges the
  payload; countable criteria (paragraphs, words, sections, items) are
  MEASURED by the engine against it (`brain/criteria.py`) — measured
  violations void approvals, surface as `criteria_format_violation`, and are
  authoritative over verdict prose. Sentence counts stay advisory.
- **Per-criterion state**: the reviewer emits a fenced `criteria` JSON table
  (met/unmet/unknown + the evidence reference that settles each); it lands in
  the authoritative verdict record. Planner is barred from compiling circular
  criteria ("the review approves…").
- **Perception for non-text observation**: image files a tool produces return
  to the agent as image content blocks (bounded count/size), and
  verification-role agents get `view_evidence` (images shown AS images, text
  as excerpts) — visual observation strategies (rendering, screenshots,
  frames, scans, charts) become executable instead of "(N bytes, binary)"
  black holes.
- **Strategy space anchored to the target, not the toolbox**: planning starts
  from a TARGET SIGNAL MODEL (what the target IS; which of its constituent
  signals carry each required property); the candidate comparison MUST
  include direct observation and a property-preserving derived representation
  (rejecting them requires naming the concrete blocker, recorded as a
  limitation); candidates are graded on an evidence-tier ladder (direct >
  derived > primary testimony > index snippets > secondary testimony >
  memory), lower tiers win only with recorded justification; BELIEF HYGIENE —
  prior conclusions/world knowledge are hypotheses that may order channels
  but never rule them out unverified.

### Deferred (tracked)
Resolution of redirects/mirrors in target-tier matching (degrades to proxy,
never the reverse); deterministic sentence segmentation; audio/other
modalities in perception (images first); retained-payload deduplication for
courier reuse.

## [0.6.0] — 2026-07-12

The failure-recovery release, from the v0.5.1 rerun of the remote-source test.
The exposed boundary: the loop could adapt only after a RUNNING agent reported
an evidence gap — when capability construction itself failed, no agent ever
existed, three same-method rebuild attempts escaped as a raw traceback, and no
alternative strategy was considered. Tool authoring was a terminal
precondition, not an action inside the cognitive feedback loop.

### Added
- **Method failures are controller observations (P0-A/E/F)**: any stage
  failure — including acquisition dying before an agent starts — is classified
  into a typed `MethodOutcome` (phase, failure class, per-attempt history,
  retained partial evidence), recorded on the blackboard, and routed to the
  SAME strategy adjudicator that serves evidence gaps. Replacement stages are
  grafted transactionally: dependents and any pending review rewire to the new
  terminal producer; the failed node survives as failure history, never as
  evidence; partial observations are preserved and surfaced, never
  re-established blindly. An empty extension is a calibrated stop. Bounded by
  `RECOVERIES_PER_RUN`; a failed method is suppressed for the recovery cycle
  unless the planner re-justifies it with a new contract.
- **`CapabilityNeed` separated from the stage goal (P0-B)**: the planner
  compiles an instance-neutral authoring contract per new capability
  (`capability_needs`), with the concrete task values riding separately as
  verification inputs; the fallback path scrubs the stage goal of task residue.
  A URL and a ten-sentence output policy were observed compiled into three
  generations of an unusable observer-and-summarizer. Planner is instructed to
  atomic roles — never fetch-and-summarize monoliths.
- **Operational verification ≠ instance yield (P0-C)**: honest negative
  observation (exit 0, `coverage='none'`, unresolved needs named) is a valid
  result by contract; deterministic evidence checks reject contradictory
  yield (`complete` + unresolved needs), observation-role output missing yield
  fields, and remote observers claiming positive yield without `source_refs`
  or retained raw payloads — at initial authoring, not only revalidation
  (P0-D). Attempt 3 would have relabeled description metadata as transcript
  text with length-based `coverage=complete`.
- **Strategy adjudication at initial planning (P0-F)**: the initial
  `strategy_rationale` is required, persisted to the blackboard, and traced —
  the initial method choice was largely unauditable.
- **Failure-aware negative memory (P1-A, scoped)**: exhausted authoring
  records the need key, failure class, and reason in the registry; per-attempt
  history rides on `AcquisitionError` so recovery sees whether attempts were
  still learning anything.
- **Catalog qualified before planning (P1-B)**: the planner's known-capability
  view excludes quarantined tools, labels stale contracts, normalizes residue,
  and lists previously-failed needs with their failure — re-selecting a dead
  end becomes a deliberate act, not amnesia. Quarantine now normalizes the
  stored description on the failure path too.
- **Transactional capability builds (P1-C)**: candidates build under
  attempt-scoped tags and take over the canonical tag only by atomic retag
  after verification (unverified attempts were overwriting the canonical local
  image); Dockerfile downloads must assert their results by contract.
- **One authoritative terminal state (P1-E)**: classification starts
  `unassessed`; a run where nothing completed finalizes through
  `run.dimensions`/`run.classification` as `FAILED` with recovery and
  deliverable dimensions, and the user receives a controller diagnosis — what
  was tried, what remains open, why execution stopped — while the raw
  traceback stays in the trace. No `DELIVERABLE.md` is written when no
  deliverable exists.
- **Decision-aware model routing (P1-D, first step)**: seeded capability
  repairs route to the fast tier; strategy/authoring stay on strong.

### Deferred (tracked)
Automatic residual-requirement conversion of mid-agent partial yield (today:
engine note + agent-declared gaps), cross-run retry-rationale enforcement
beyond the qualified-catalog label, full model-identity telemetry.

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
