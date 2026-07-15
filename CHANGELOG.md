# Changelog

All notable changes to The Project Brain are recorded here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versioning is
[Semantic Versioning](https://semver.org/) — `0.x` while the core algorithm
and behavior are still expected to change between releases.

## [0.18.0] — 2026-07-15

The review-targeting + designation release, from campaign c20260715-0608
cycle 1 (the first cycle run on v0.17.0). The cycle's headline: three of
four failures were the engine pointing a working mechanism at the wrong
object. The review pattern assumed its first dependency was the draft
under review — an order the planner contract never promises — so two runs
whose content every verdict round verified spent all their rounds judging
a prerequisite stage's status report and routing revisions to an agent who
correctly answered "not my task"; the artifact-of-record rule measured
whichever measurable artifact appeared LAST, which after v0.17's
collision-deduped output names meant template-probe placeholders, not the
report the payload shipped; and a session's second turn blocked honestly
because the prior turn's deliverable — the referent of the user's feedback
— was never provisioned when both revision detectors (the LLM judgment and
the prior-path fallback) were defeated by a planner that inlined the prior
text into a stage goal. The fourth failure repeated a known class: a
review-round revision regenerated its figures from memory and fabricated
values its own round-3 reviewer caught.

### Fixed
- **Review subject selection** (`workflow.review_subject`): a review stage
  judges, and routes revisions to, the first deliverable-MARKED dependency
  when one exists (the same authority rule as `deliverable_stage_ids`),
  falling back to the first dependency. The review's evidence block
  assembles from every non-subject dependency instead of `deps[1:]`.
- **Measurement designation** (`_measurement_target`): among produced
  artifact-role files, one the deliverable payload references (by path or
  bare filename) is the deliverable of record; recency only breaks ties
  among referenced files and still rules when the payload references none.
- **Session-turn provisioning**: a run that continues a session always
  seeds the previous turn's deliverable as retained, target-tier prior
  evidence — provisioning no longer depends on the planner's revision
  judgment or on a `prior/` path appearing in the plan; those remain as
  trace signals only.

### Added
- **Minimal-edit rule** in both revision (redo) prompts: the fix session
  must change exactly what the critique names and carry everything else
  over from the previous draft verbatim — regeneration from memory is the
  documented fabrication channel. Prompt-level; every gate still applies.

## [0.17.0] — 2026-07-15

The review-convergence + measurement-truth release, from the campaign's
cycle-3 report (owner-approved fix queue). Cycle 3 proved every v0.16.0
change did its mechanical job and exposed the next wall: reviews acted but
did not CONVERGE, and the single largest cause was a load-bearing
deterministic check with wrong semantics — the section counter counted the
document's H1 title as a section, so a fully compliant deliverable (title +
exactly N sections) measured N+1 VIOLATED, and reviewers, contractually
bound to trust engine measurements over their own eyes, burned every
bounded round chasing a phantom. Two compliant, factually strong runs
shipped `unverified` on exactly this. The rest of the queue: measurement
aimed at the wrong object, revisions that traded one measured defect for
another, exhausted reviews holding critiques nobody could apply, a revision
run blocked by one silent LLM judgment the plan itself contradicted, cached
tools serving alien interfaces or overwriting their own outputs, and
evidence lookups denying text that was verbatim in the corpus.

### Fixed
- **Section counting semantics** (`criteria.py`): sections are counted at
  the body's top heading level; a single leading heading strictly shallower
  than every other heading is the document TITLE, not a section; deeper
  subheadings are structure inside a section, not more sections. The
  `heading` unit still counts every heading. The counting rule is STATED
  (`SECTION_RULE`) wherever measurements are shown — authors and reviewers
  see what the engine counts, not just the number.
- **`locate_evidence` recall**: matching is typography-normalized (curly
  quotes, en/em dashes, PDF ligatures, soft hyphens, markdown emphasis
  markers are transparent), decoding is tolerant (`errors="replace"` with a
  binary guard — one bad byte no longer hides a whole evidence file), and
  when the exact phrase misses, an ordered token-window pass reports
  clearly-labeled APPROXIMATE matches. NOT FOUND keeps meaning "not even
  approximately present", so a miss stays trustworthy.
- **Output collision discipline** (`runtime._save_outputs`): a tool call
  can no longer destroy an earlier call's distinct output — identical bytes
  rewrite in place, different bytes land at a deduped name, and the
  returned paths are what provenance records (a chart tool writing one
  fixed path was observed overwriting chart after chart).

### Added
- **Measurement follows the deliverable of record**: when the run produced
  a deliverable ARTIFACT file in a text-measurable format (declared
  artifact role; HTML measured through a deterministic countable-text
  projection), count criteria are measured against the artifact — the
  payload may faithfully point at it. Facts blocks, approval-gate voids,
  and the terminal classification all name the measurement target; the
  author contract states it. Closes the payload-shape grading lottery
  (10/11 → 6/11 on unchanged analysis quality).
- **Deterministic dry-runs before the expensive verdicts**: (a) a
  deliverable author whose draft violates a measured count constraint gets
  the measurement and one immediate corrective pass at authoring time
  (`criteria.presubmit_violation` / `stage.format_fixup`), instead of
  spending a bounded review round relaying a deterministic fact; (b) a
  reviser that fixes the critiqued items but introduces a NEW measured
  violation gets one corrective pass in-round (`review.revision_remeasured`
  / `review.revision_fixup`); (c) an approval that would void on FIXABLE
  claims-table mechanics (missing table, citation that does not resolve)
  gets one re-emission exchange checked by the UNCHANGED gate
  (`review.claims_fixup`) — substantive defects (unsupported claims,
  no grounded support) still void outright, never invited to retype.
  A fix attempt that measures worse is discarded in every path.
- **Post-review redo, bounded** (`POST_REVIEW_REDOS=1`): a review that
  exhausts its rounds on a final `revise` verdict holding an
  author-actionable critique applies it once and buys exactly ONE more
  fully-gated round (`review.post_redo`). Evidence-absence voids are
  excluded — no redo can retain evidence. Closes the three
  `redo_unavailable` drops per cycle.
- **Deterministic revision fallback**: a plan or objective that references
  `prior/` run paths in a seeded session IS the revision signal — the prior
  deliverable is seeded as target-tier evidence even when the planner's
  `revision` judgment silently fails (`revision.fallback_detected`); the
  single unchecked LLM emission is no longer an availability SPOF.
- **Cached-tool interface conformance**: at reuse resolution, every key of
  the stage's example input must be accepted by the cached tool's declared
  schema, checked deterministically before any container runs
  (`capability.contract_mismatch` → re-author); a cached writer serving its
  birth task's bespoke record schema can no longer ride in on a
  generalized description.
- **Markup integrity at verification** (probe epoch → 5): produced `.html`
  output that is truncated mid-tag, holds an unclosed `<script>`/`<style>`,
  or shows a structural open/close imbalance ≥ 2 fails the build with the
  defect named — the third-cycle-running malformed report generator class
  dies at birth. Developer contract: derive output filenames from input
  identity (never one fixed path); write markup as one complete document.

## [0.16.0] — 2026-07-15

The see-your-own-file-state release, from the external campaign's second
cycle (two runs converted to full successes on the v0.15.0 changes; honesty
gates held everywhere). The cycle's remaining losses shared two structural
causes. First, agents had NO way to enumerate the files their own run holds:
one worker was told its workspace was empty while every file it needed sat
under a seeded `prior/` directory the miss-message listing excluded, and
another read a first-40-of-129 truncated listing as proof the files beyond
it did not exist — a false absence observation manufactured by the engine's
own error text. Second, planner-written reviews carrying `rounds: 1` (the
planner contract read "fix cycles"; the executor counts verdict rounds)
turned every first-round "revise" into instant exhaustion — three reviews
wrote precise, actionable critiques that were never applied.

### Added
- **`list_files` workspace tool**: deterministic, glob-filterable (full
  path and basename), paged enumeration of everything the run holds, with
  sizes, a total count, and truncation labeled AS truncation pointing at
  the next offset. Available to every stage agent alongside `read_file`.
- **Workspace inventory in agent prompts**: each session's task prompt
  carries an engine-computed rollup of the run's file state (per-directory
  count with first/last names, `prior/` seeds included) — a fact handed to
  the agent instead of a layout it must guess.

### Changed
- **read_file miss messages correct the agent instead of misleading it**:
  nearest-match candidates for the requested name, `prior/` included, a
  per-directory rollup that reveals the far end of long sequences, and
  explicit framing that a bounded listing is never evidence of absence.
- **Review stages floor at 2 verdict rounds** (`min(2, REVIEW_MAX_ROUNDS)`),
  so a first-round revise verdict always gets one fix cycle and one
  re-verdict; the planner contract now states the verdict-round semantics.
  Acceptance unchanged — approval still requires an approved verdict.
- **Unhonorable `redo_stage` directives are visible**: when the supervisor
  orders a redo the engine cannot run (no redo registered — review stages
  never have one — or already redone), it emits
  `supervisor.redo_unavailable` instead of silently dropping the order.

## [0.15.0] — 2026-07-14

The round-survival and evidence-provisioning release, from the first
five-task external campaign (unfamiliar domains, deterministic grading). No
run overclaimed — the honesty gates held everywhere — but three of five runs
shipped UNVERIFIED for reasons the engine itself caused: reviews correctly
refused to approve while the engine failed to provision the evidence the
approval needed or to survive its own infrastructure faults. One run died
whole when a planner burned its turn budget on malformed tool arguments that
were answered "plan recorded"; another re-attempted one failing approach
three times under three freshly-named contracts.

### Added
- **Corpus manifest**: before each review round the engine materializes a
  deterministic enumeration of the retained target-lineage evidence as a
  citable workspace file, recorded as consuming those files (lineage:
  `derived`). A negative claim scoped to the corpus ("no X exists in the
  sources") can now cite real evidence — a complete enumeration IS the
  observation that grounds within-corpus absence. The claims gate is
  unchanged; a miscited absence claim still voids. The manifest is excluded
  from the `target_evidence` metric (bookkeeping must not inflate it).
- **Prior deliverable as evidence in revision runs**: deliverables are agent
  text — never retained by a tool, so never in any workspace — which made
  preserve-discipline criteria ("unchanged except…") structurally
  unverifiable. A revision run now seeds the prior run's deliverable under
  `prior/<run>/outputs/` and records it `target`-tier
  (`evidence.record_designated`): the artifact under revision is a
  designated source by construction. Non-revision continuity seeds do NOT
  import it.
- **Run-scoped, cross-contract failure memory**: authoring failures
  (build/verify) append to a per-run log shown to every subsequent developer
  session — per-name memory alone let an identical approach fail again under
  every new name a replan invented. The persistence digest adds a
  deterministic repeated-signature count (hex/digits normalized) so the
  supervisor sees "seen 3x" instead of misreading repeats as distinct bugs.
- **Delegated-work beacon** (`observability.activity`): research,
  upstream-question sessions, sandbox runs and image builds mark an
  in-flight window; the stall watchdog treats a session waiting on that work
  as active instead of killing it mid-wait (observed: a developer killed at
  180s while its own requested research was still running — the finished
  research discarded with the cascade). Total session budgets are never
  suspended, and the watchdog now parks the pending stream read across
  tolerated waits instead of cancelling the underlying generator.

### Fixed
- **Frame-share result budgets**: the SDK's 1 MiB limit applies to the
  MESSAGE, and one assistant turn's PARALLEL tool results ride a single
  frame — two results each inside the old per-result budget summed past the
  limit and killed a 1-round review session. Every per-result budget is now
  the frame budget divided by `TRANSPORT_FRAME_SHARE` (default 3).
- **Review round refunds**: a round that dies of infrastructure (an
  exception, not a verdict) is retried without consuming the round budget,
  bounded by `REVIEW_CRASH_RETRIES` (default 1). With a 1-round review, one
  transport fault previously equaled review death and the draft shipped
  unverified with its review never performed.
- **Plan-emission repair and salvage**: `emit_plan` calls with no recognized
  parameter get a corrective error naming the expected keys (previously
  answered "plan recorded" — positive feedback for capturing nothing); a
  lone unrecognized envelope key whose value contains the real payload is
  deterministically unwrapped; and a validly captured plan now survives the
  planner session's death instead of being discarded with the exception.
  Planner sessions get 6 turns (was 4).
- **Authoring contract — guarded zero-yield path**: tools must code
  aggregations over possibly-empty collections defensively so genuine
  emptiness reaches the honest `observed_absent`/`not_detected` exit instead
  of crashing (observed: a crash on "nothing there" converted the most
  decisive observation available into a fake malfunction, retried for half
  the run). Review guidance now also directs numeric claims to be
  cross-derived from structured source data before "uncontradicted" is
  assertable, and absence-within-corpus claims to cite the manifest.

## [0.14.0] — 2026-07-14

The transport-safety and anchored-deliverable release, from the first
session-revision trial. That run proved the v0.13 architecture end-to-end —
feedback recognized as a revision, prior artifacts reused, mid-authoring
library research, two persistence-judged recoveries, and a dramatically
better artifact — yet still shipped UNVERIFIED: the SDK's 1 MiB transport
frame killed three sessions including the final review round, the crashed
review aborted holding an unapplied 3-defect diagnosis, an extension made the
INSPECTOR the deliverable owner (its report shipped as DELIVERABLE.md), a
broken tool ended as a silent quality downgrade, and the revision run's
seeded sources carried no lineage (target_evidence: 0).

### Added
- **Agent-reported tool defects route to repair**: a new `report_tool_defect`
  team tool records "my tool crashes / cannot express what quality requires"
  as controller state; the engine repairs the capability (existing
  `repair_capability`, research-assisted) and re-runs the reporting stage
  once with the fixed tool — bounded by `TOOL_DEFECT_REPAIRS_PER_RUN`.
  Failure-driven repair existed; quality-driven repair did not (observed
  twice: charts silently degraded to tables/bullets behind a blocker note).
- **Deliverable ownership is plan-level state**: stages carry
  `deliverable: true` (the producer, never an inspector); planner-marked
  ownership overrides the graph-tail heuristic in `deliverable_stage_ids`,
  extension retargeting keeps the OWNER as the review's draft (new stages
  join as evidence), and DELIVERABLE.md ships from the last completed owning
  stage — never implicitly from whatever stage ran last.
- **Seeded lineage inheritance**: each run persists its designated locators;
  seeding a prior run now translates its provenance records to
  `prior/<run_id>/…` paths with each file's tier as computed IN THAT RUN
  (prior user inputs stay `target`), the ledger honors inherited tiers and
  lists seeded evidence, and lineage propagates through them — a revision
  can finally certify traceability to the original materials.
- **Fix rounds carry lessons forward**: a fix round's working notes
  (tool-behavior discoveries) are accumulated per stage and shown to later
  fix rounds — the auto-added-cover-slide lesson was learned and lost across
  sessions, and the trap re-fired.
- **Producers get the reviewer's eyes**: deliverable stages reuse the
  promoted render capability when one exists (reuse only, never an authoring
  stall), so the self-check can actually LOOK at a binary artifact.

### Fixed
- **Transport-safe tool results**: every result — text plus encoded images —
  is now assembled under `TRANSPORT_SAFE_RESULT_BYTES` (the SDK stdio frame
  rejects messages over 1 MiB and kills the whole session). Oversized images
  are recompressed/downscaled to fit (Pillow), unbounded stdout is head/tail
  clipped with the meta line preserved, cumulative image budgets replace
  per-image-only caps, and `read_file` never falls through to a text read of
  image bytes. Observed: rendered-page results killed three stages including
  a final review round.
- **A crashed review round degrades instead of aborting the review**: the
  round is recorded as `errored`, consumes one round, and the next round
  proceeds with guidance to work economically; courier and fix-round crashes
  likewise leave the draft standing. Observed: one crash shipped the draft
  UNVERIFIED with a precise defect list never applied.
- **Perception freshness is gated**: renders are recognized by SHAPE
  (images produced from a renderable input — any tool, any name); a render
  outrun by artifact regeneration is stale — stale page counts measure
  nothing, the reviewer is told to re-render, and approvals over stale or
  absent perception (with the render tool available) are VOIDED. Observed: a
  review round critiqued a superseded draft from stale images while the
  terminal gate certified a slide count the delivered artifact violated.
- **Artifact hygiene is role-scoped**: files whose producing tool declared a
  non-artifact role (observation/derived/validation) are exempt from
  deliverable-hygiene scanning — a research agent's fetched pages were
  polluting terminal classification with "artifact defects".
- **Capability dedup guidance**: the planner must reuse an existing tool
  whose contract covers a need instead of declaring a synonym (a second page
  renderer was authored while the first sat promoted and in use).

## [0.13.1] — 2026-07-13

### Fixed
- **v0.13.0 was dead on arrival — every run crashed at the first plan call**
  with `KeyError: 'topic, why'`: the new planner prompt sections contained
  literal `{topic, why}` / `{of_run, …}` braces, and the template goes through
  `str.format()`, which parsed them as replacement fields. Braces escaped; a
  regression test now RENDERS every formatted prompt template and asserts its
  declared fields are exactly the ones the engine supplies — the suite had
  stayed green because nothing executed the real template (the same
  stub-confidence gap as the v0.9.1 perception crash).

## [0.13.0] — 2026-07-13

The quality-loop release, from the first full artifact-production trial. That
run validated v0.12 (first-try tool verification over real binary inputs, a
mid-run capability gap closed by authoring) yet shipped an amateur deliverable
UNVERIFIED — every adaptation trigger in the engine was failure-shaped, so a
run that was grounded and criteria-clean but mediocre triggered nothing:
"what does good look like?" was never asked, no one ever LOOKED at the
audience-facing artifact, and the review died wholesale when one of its tools
failed to build. This release gives the loop a quality sense and a session a
memory.

### Added
- **Knowledge needs — define GOOD before building**: plans now declare
  `knowledge_needs` ({topic, why}) for audience-judged deliverable forms whose
  quality standards the objective does not spell out. The engine resolves them
  through the existing research channel (same per-run budget) and re-invokes
  the planner ONCE with the briefs; the informed plan encodes the researched
  standards as checkable success criteria. The briefs also reach the
  deliverable-producing stages as "RESEARCHED QUALITY STANDARDS" — the bar the
  work is judged against. One pass, fail-open, fully traced
  (`knowledge.researching` / `knowledge.informed` / `knowledge.replan_failed`).
- **Perceptual review — if an audience will look at it, the reviewer must look
  at it**: files whose producing tool DECLARED `emits: artifact` (never
  classified by extension — retained evidence like a fetched page is input to
  judgment, not the thing judged) put the review stage in perceptual mode. A
  generic `render_artifact_pages` capability need (document/page-layout file →
  one PNG per page) is seeded engine-side and authored on first use like any
  tool; the reviewer must render, open EVERY page, and judge craft: layout
  integrity, data-graphic honesty, consistency, audience-copy discipline.
- **Rendered page count is authoritative**: the engine measures slide/page
  count criteria from the latest render's provenance record — the payload-text
  heuristic stands only when nothing was rendered. Both directions matter: a
  stray separator cannot void a correct artifact, and clean prose cannot
  excuse an 11-page artifact against an explicit 10. Wired into the review
  approval gate, the reviewer's measured-facts block, and terminal
  classification.
- **Session revision memory**: the interactive session now remembers its
  previous run (objective, result, deliverable payload head, produced files)
  and passes it to the planner as SESSION CONTEXT; the previous run of THIS
  session outranks the relevance-picked cross-run seed, so its artifacts are
  seeded under `prior/<run_id>/`. The planner judges whether the new input is
  FEEDBACK on that deliverable and, if so, plans a `revision` ({of_run,
  feedback, preserve}): load the prior artifacts, change only what the
  feedback targets, keep what the user already accepted, carry the original
  criteria plus the feedback as new ones. Auditable via the `plan.revision`
  trace and blackboard entry.
- **Self-check before handoff**: the deliverable contract now requires the
  producing agent to perceive its own artifact the way the audience will
  (render or open images) and fix what it sees before finishing.

### Fixed
- **Review capability failures degrade instead of killing the review**:
  capabilities for verification-role sessions are acquired per-tool,
  best-effort; a failed authoring becomes a `review.capability_degraded` trace
  plus an explicit note to the reviewer naming what is missing — observed: one
  failed tool build aborted the entire review and the draft shipped UNVERIFIED
  with no substantive critique. Same treatment on the evidence-courier path.
- **Authoring file-reference errors now enumerate the workspace**: a
  `sample_input.files` reference that does not resolve lists the files that DO
  exist (prior-run seeds excluded) — observed: a developer guessed the same
  wrong path three times and died, with the real file sitting one listing
  away.

## [0.12.0] — 2026-07-13

The real-input verification release, from the first archive-input trial. The
run's control plane worked — the persistence judgment fired live for the first
time, correctly detected a three-identical-failures plateau, stopped, and asked
— but the underlying failure was an engine gap, and the ask was misdiagnosed.

### Added
- **Verification against REAL run files**: `sample_input.files` (workspace-
  relative paths) are now mounted into the verification sandbox at their
  basename, exactly as at run time. Previously those references were silently
  DISCARDED — the only fixture channel was text-only `sample_files` — so any
  tool whose genuine input is a real/binary file (archives, spreadsheets,
  recordings, database dumps) could never see its input at verification and
  failed 'file not found' regardless of code quality, while a healthy input
  sat in the workspace. Missing/traversal/unreadable references fail with
  instructive errors; refs covered by a synthetic fixture are satisfied by it.
  The developer contract and the planner's `example_input` guidance now route
  file-consuming tools to reference the task's real files; the workspace is
  threaded through `acquire`/`repair_capability`.
- **REPL multi-line objectives**: `readline` is enabled (bracketed paste — a
  multi-line paste lands as ONE objective instead of submitting at the first
  newline, plus history/editing), and a terminal-agnostic `\"\"\"` block form
  reads until a closing `\"\"\"`.

### Fixed
- **File-related judgments can now see the user's files**: the persistence
  digest and the structured help request include the `input/` inventory (with
  sizes) — a stop-and-ask was observed telling the user to re-upload a file
  that was present all along, because its digest excluded `input/`.

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
