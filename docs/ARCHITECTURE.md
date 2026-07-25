# The Project Brain — core algorithm architecture

Technical structure of the Brain core, current as of **engine 0.28.0**. The
diagrams and the budget table were written at 0.11.0 and re-verified against
`brain/config.py` at 0.28.0; §11 lists what is still deliberately open. This
document explains how the system thinks; `OVERVIEW.md` explains how to run it, and
`CHANGELOG.md` records how each mechanism was earned (nearly every box below
exists because a live run failed without it).

---

## 1. Design invariants

Four rules bind every mechanism in this document:

1. **The core loop is law.** Every run is `analyze → decompose → identify
   requirements → generate agents/tools → execute`, and the loop may RE-ENTER
   itself mid-run (new requirements discovered during execution). There are no
   fixed pipelines and no scenario-specific branches anywhere in the engine:
   nothing in `brain/` mentions a task domain, and capabilities are identified
   by their input/output **contract**, never by name.
2. **Honesty floor.** Nothing is ever fabricated to look successful: evidence
   is what a tool retained, not what an agent narrated; absence of yield is
   never proof of absence; verdicts are gated deterministically; runs end
   `unverified`/`blocked`/`failed` rather than pretend. Capability trust is
   *earned* (verify-before-promote) and *revocable* (revalidation, quarantine).
3. **Problem-solving layer on top of the floor, never instead of it.** The
   system researches its own knowledge gaps, builds missing tools, replans
   failed methods, and persists while progress is real — but still only claims
   what it verified.
4. **Engine vs. output.** The engine is `brain/` (tracked). Everything a run
   produces — traces, workspaces, authored tools, the capability library — is
   an output under `data/` (git-ignored). A run never writes to tracked files.

---

## 2. Bird's-eye component map

```
                                  USER
                    objective │ approvals │ secrets │ files
                              ▼
┌─ ENTRY ──────────────────────────────────────────────────────────────────┐
│  main.py (one-shot)   repl.py (interactive)                              │
│              └────────────┬─────────────┘                                │
│                     session.py  BrainSession                             │
│   auth invariant · janitor · secret-shape refusal · file designation     │
│   (@mention / named path / inbox, ask-if-unreachable) · continuity block │
│   · qualified capability catalog · component wiring                      │
└──────────────────────────────┬───────────────────────────────────────────┘
                               ▼
┌─ CONTROL ────────────────────────────────────────────────────────────────┐
│                     orchestrator.py  Orchestrator                        │
│        the executive loop: plan accountability → stage execution →       │
│        adaptive transitions → deterministic gates → terminal state       │
│                                                                          │
│   planner.py ◄──── replans/extensions ────┐    supervisor.py             │
│   (plan / extend)                          │    (consult · persistence)  │
└───────┬──────────────┬─────────────────────┴──────────┬──────────────────┘
        ▼              ▼                                ▼
┌─ CAPABILITY PLANE ─────────────┐   ┌─ EVIDENCE & TRUST PLANE ────────────┐
│ resolver.py   reuse vs create  │   │ evidence.py   lineage ledger        │
│ acquisition.py authoring loop, │   │ criteria.py   measured constraints  │
│   contracts, probes, epochs,   │   │ review gates  claims + criteria     │
│   atomic promotion, repair     │   │ workflow.py   deliverable payload   │
│ registry.py   library + memory │   │ team.py       gap/blocker signals   │
│ verifier.py   recipe checks    │   └─────────────────────────────────────┘
│ capabilities/runtime.py        │
│   caps-as-MCP-tools, provenance│   ┌─ SAFETY SUBSTRATE ──────────────────┐
│   recording, image perception  │   │ sandbox/ (socket-proxy, clamps,     │
│ capabilities/health.py budgets │   │   ledger, OOM, leak detection)      │
└────────────────────────────────┘   │ approval.py (gate, secrets, input)  │
                                     │ factory.py  (locked agent sessions) │
┌─ MEMORY & OBSERVABILITY ────────┐  │ observability/redact.py (scrubbing) │
│ memory/blackboard.py run state  │  │ watchdogs (stall/total timeouts)    │
│ memory/history.py continuity    │  └─────────────────────────────────────┘
│ observability/ tracer, viewer,  │
│   OTel/Phoenix export           │
└─────────────────────────────────┘
```

---

## 3. The core loop (with every feedback edge)

The orchestrator is a closed loop, not a pipeline. Solid arrows are the happy
path; labeled return edges are the adaptive transitions that make the system
recover instead of dying.

```
 objective (+ designated sources: attached files, named paths, remote locators)
     │
     ▼
 ┌────────────────────────────┐   proxy-first plan (source-derived, direct
 │ PLAN        planner.plan() │   observation skipped, no legitimate blocker)
 │  target signal model       │◄──────────────────────────────┐
 │  candidate methods + tiers │   ONE corrective replan        │
 │  direct_observation decl.  │──── accountability gate ───────┘
 │  stages DAG · criteria     │    (_correct_proxy_first_plan)
 │  capability_needs ·        │
 │  expectations              │
 └─────────────┬──────────────┘
               ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │ FOR EACH STAGE (topological order; the list can GROW mid-run)        │
 │                                                                      │
 │  ┌─────────────────┐  need    ┌──────────────────────────────┐       │
 │  │ RESOLVE          │────────►│ ACQUIRE (capability plane)   │       │
 │  │ reuse? create?   │         │ author → verify → promote    │──┐    │
 │  └───────┬─────────┘          └──────────────┬───────────────┘  │    │
 │          │ tools ready                       │ AcquisitionError │    │
 │          ▼                                   │ (classified)     │    │
 │  ┌──────────────────────────────┐            │                  │    │
 │  │ EXECUTE  locked agent session │            │   request_research   │
 │  │ single | map | reduce | review│◄───────────┼──────────────────┘    │
 │  │ team channel · read_file ·    │            │   (mediated, budgeted,│
 │  │ caps tools · health monitor   │            │    cached — reaches   │
 │  └───────┬──────────────┬───────┘            │    stage AND developer│
 │          │              │ degraded tool      │    agents)            │
 │          │              ▼                    ▼                       │
 │          │      ┌───────────────┐   ┌─────────────────────────────┐  │
 │          │      │ REPAIR tool    │   │ METHOD FAILURE              │  │
 │          │      │ re-author with │   │ → MethodOutcome (typed:     │  │
 │          │      │ live failure,  │   │   phase, class, attempts,   │  │
 │          │      │ re-run agent   │   │   history, partial evidence)│  │
 │          │      └───────────────┘   └──────────────┬──────────────┘  │
 │          ▼                                         ▼                 │
 │  ┌────────────────┐                 ┌──────────────────────────────┐ │
 │  │ SUPERVISOR      │                │ PERSISTENCE JUDGMENT          │ │
 │  │ consult (redo/  │                │ keep_trying │ stop_and_ask │  │ │
 │  │ inform/proceed) │                │ stop   (strong model tier)   │ │
 │  └────────┬───────┘                 └──────┬───────────┬───────────┘ │
 │           ▼                          keep  │           │ ask/stop    │
 │  agent reports                             ▼           ▼             │
 │  report_evidence_gap ──┐   ┌────────────────────┐  structured HELP   │
 │  (observation the      │   │ RECOVER             │  REQUEST /         │
 │   objective needs but  │   │ planner.extend() →  │  calibrated stop   │
 │   tools can't observe) │   │ graft replacement   │                    │
 │           │            │   │ stages, rewire      │                    │
 │           ▼            │   │ dependents + review │                    │
 │  ┌──────────────────┐  │   │ (failed method      │                    │
 │  │ EXTEND            │◄─┘   │  suppressed unless  │                    │
 │  │ planner.extend() │      │  re-justified)      │                    │
 │  │ new stages before│      └────────────────────┘                    │
 │  │ pending review;  │                                                │
 │  │ review retargeted│   review found unmet evidence + empty lineage  │
 │  │ or RE-APPENDED   │◄───────────────────────────────────────────────┤
 │  └──────────────────┘        (courier couldn't retain → escalate)    │
 │                                                                      │
 │  ┌──────────────────────────────────────────────────────────────┐    │
 │  │ REVIEW (evidence-grounded critic — see trust plane, §6)      │────┘
 │  └──────────────────────────────────────────────────────────────┘
 └───────────────────────────────┬──────────────────────────────────────┘
                                 ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │ TERMINAL STATE  _classify() — one authoritative state machine        │
 │ dimensions: stage completion · grounding · criteria · artifact ·     │
 │ blockers · recovery · deliverable · method failures · target evidence│
 │ → succeeded | unverified | partial | blocked(+help request) | failed │
 │ outputs/DELIVERABLE.md = pure payload   ·   ANSWER.md = full envelope│
 └──────────────────────────────────────────────────────────────────────┘
```

Bounded everywhere: replans, recoveries, research requests, review rounds,
courier fetches, repairs, sessions, and container resources all have hard
ceilings (§8) — judgments decide *earlier* stops; ceilings guarantee an end.

---

## 4. Planning layer

`planner.py` runs a locked LLM session that must emit one structured plan
(`emit_plan` tool; the engine — `workflow.normalize_stages` — guarantees
structural validity, so the model cannot produce an unexecutable graph).

A plan carries:

| Field | Purpose |
| --- | --- |
| `stages` (DAG) | `single \| map \| reduce \| review` topology chosen per objective; map width is decided at RUN time from the upstream manifest, clamped by config |
| `success_criteria` | checkable, objective-derived; **semantic decomposition** per requested dimension (events, mechanism, topic/theme, evaluation, chronology); never mechanism-bound, never circular ("review approves…" is banned) |
| `strategy_rationale` | audited method choice: **target signal model** (what the target IS, which constituent signals carry each required property) → candidate methods → evidence-tier grading (direct > derived > primary testimony > snippets > secondary > memory) → belief hygiene (priors may order channels, never rule them out unverified) |
| `direct_observation` | accountability declaration: `planned` or `infeasible` with a **closed blocker enum** — `requires_credential / resource_ceiling / no_free_means / physical_access / user_authority`. "No tool exists yet" is *inexpressible*: the system authors tools, so tool-absence can never justify a proxy |
| `capability_needs` | per new capability key: an **instance-neutral contract** (what it does — no task URLs, counts, or synthesis policy) plus `example_input` (concrete task values, used for verification only) |
| per-stage `expectation` | what a successful observation should yield — the baseline the persistence judgment compares actual yield against |

The orchestrator enforces accountability before execution: a source-derived
plan that skipped direct observation with no legitimate blocker triggers **one
corrective replan** (`plan.proxy_first` → `plan.corrected`), fail-open.

`planner.extend()` re-enters the same schema mid-run for evidence gaps and
method-failure recovery, with two extra rules: *failed methods are evidence*
(don't re-select without new justification) and *criteria recompilation*
(method-bound assumptions may change; the user's demands may not).

---

## 5. Capability plane — tools are built, verified, then trusted

```
        stage needs capability "k"
                  │
                  ▼
         ┌─── resolver.py ───┐
         │ registry lookup    │
         └──┬────────────┬───┘
      REUSE │            │ CREATE (or quarantined/suppressed → CREATE)
            ▼            ▼
 ┌────────────────┐   ┌───────────────────────────────────────────────┐
 │ REVALIDATION    │   │ AUTHORING LOOP (bounded attempts, classified) │
 │ epoch = engine  │   │  developer agent (locked; submit_tool +       │
 │ version + hash( │   │  progress_update + request_research)          │
 │ contract+probes)│   │      │ submission (contract-checked: emits,    │
 │ stale → probes  │   │      ▼  schema, resources, credential decl.)   │
 │ pass → restamp  │   │  build CANDIDATE image (attempt-scoped tag)    │
 │ fail → QUARANTINE────►      │                                        │
 │ + re-author     │   │      ▼                                        │
 └───────┬────────┘   │  GENERIC VERIFY (_generic_verify)              │
         │            │   sample run · JSON contract · consumed_inputs │
         │            │   · evidence contract (coverage, observation_  │
         │            │   state, source_refs, retention, contradiction │
         │            │   checks) · fabrication probes (no-input,      │
         │            │   irrelevant-input)                            │
         │            │      │ pass                    │ fail          │
         │            │      ▼                         ▼               │
         │            │  ATOMIC PROMOTION          error TAIL fed back │
         │            │  retag candidate → canonical;  to developer;   │
         │            │  description generalized;      next attempt    │
         │            │  registry.add + build context  (or research)   │
         │            │      │            exhausted → AcquisitionError │
         │            │      │            (classified) + registry      │
         │            │      │            .record_failure (negative    │
         │            │      ▼             memory, shown to planner)   │
         └──────────► RUNTIME (capabilities/runtime.py)                │
                      │  each capability = one MCP tool; runs in a     │
                      │  fresh clamped sandbox; file bridge /work/in → │
                      │  workspace/<cap>/; provenance line; evidence   │
                      │  recording; image outputs returned AS IMAGES;  │
                      │  health monitor (ok/empty/error, call budget)  │
                      │        │ degraded in practice                  │
                      │        ▼                                       │
                      │  repair_capability (seeded with live failure,  │
                      │  fast tier) → re-verify → re-promote in place  │
                      └────────────────────────────────────────────────┘
```

The **developer contract** (hard rules, fingerprinted into the validation
epoch so changing it invalidates previously promoted tools): evidence boundary
(never fabricate/substitute input), perception-before-interpretation,
evidentiary yield (`coverage`, `unresolved_needs`), **observation state**
(§6), channel lineage (a fallback never inherits the requested property's
identity), remote retention (verbatim payloads, unique filenames,
`source_refs`), output role (`emits: observation | derived | artifact |
validation`), verbatim persistence for writer tools, honest resource
declarations, build-download asserts, and **never author perception models**
— reading text and looking at images is the agent's own multimodal ability;
tools produce the *files to look at*.

---

## 6. Evidence & trust plane — how a success is proven

**Lineage ledger** (`evidence.py`). The capability runtime records, per tool
call: files retained, locators *actually accessed*, workspace files *actually
consumed*, reported yield + observation state, producing role, and a content
hash. The ledger propagates lineage to a fixpoint:

```
 designated sources                        tiers (TARGET LINEAGE = evidence OF
 (objective locators,                      the source itself)
  attached inputs)                         ┌────────────────────────────┐
     │  direct access (source_refs match)  │ target   ── direct payload │
     ▼                                     │ derived  ── produced by    │
 [payload files] ──consumed by tool──►     │   consuming target lineage │
     transformation → [derivative files]   │   (role-independent)       │
     (samples, pages, rows, extractions)   ├────────────────────────────┤
                                           │ proxy    ── other sources; │
 other access (search indexes, 3rd-party   │   testimony ABOUT target   │
 pages) → proxy; proxy-derived stays proxy │ retained ── unknown        │
 agent prose → NEVER in the ledger         │   provenance; never proof  │
                                           └────────────────────────────┘
```

**Observation states** (absence ≠ failure, structurally):
`observed_present / observed_absent / not_detected / ambiguous /
observation_failed`, with deterministic contradiction checks. Only
`observed_absent` can support a claim that the target *lacks* something; a
processing failure may never share an output shape with genuine emptiness.

**Review protocol.** The reviewer receives: the deliverable **payload** (the
fenced block that actually ships), the tier-labeled ledger, engine-measured
count-criteria facts, the original-evidence packet (inputs + observation
outputs + target-lineage payloads), upstream analysis (marked non-ground-
truth), and deterministic tools — `locate_evidence` (exact-string lookup over
evidence files) and `read_file` (images shown as images). It must emit:

1. `VERDICT: approved | revise | needs-evidence` (unparseable → `revise`);
2. a per-criterion table (`met | unmet | unknown` + evidence reference);
3. a **claims table** — each load-bearing claim typed
   `direct | derived | inferred | negative | proxy | unsupported` with the
   ledger file that supports it.

**The deterministic approval gate** — a parsed "approved" cannot override
engine facts. On a source-derived run, approval is VOIDED when:

```
 empty ledger ─────────────► nothing retained: narration was approved
 no target lineage ────────► proxy-only ledger: testimony can't certify source
 claims gate ──────────────► missing table · any unsupported claim ·
                             zero direct/derived support · a direct/derived/
                             negative claim citing a non-lineage file
 measured criteria ────────► counted constraint violated in the payload
```

A voided approval becomes `needs-evidence`/`revise`; a **courier** may fetch
exactly the missing evidence (labeled by the *ledger diff* — narration is
never called evidence); an unresolved demand escalates into the adaptive loop
as an evidence gap, and post-review extensions get a **fresh review** so new
evidence never ships unreviewed.

---

## 7. Adaptive control — recovery, research, persistence

* **Evidence gaps** (`report_evidence_gap`): an agent declares the objective
  depends on something its tools cannot observe → `planner.extend()` grafts
  observation stages before the pending review (which is retargeted to the new
  artifact-of-record).
* **Method failures**: *any* stage failure — including capability acquisition
  dying before an agent exists — becomes a typed `MethodOutcome` (phase,
  failure class `authority/packaging/implementation/contract/submission/
  suppressed/execution/engine`, attempt history, retained partial evidence)
  on the blackboard. The **persistence judgment** (supervisor, strong tier,
  own budget) chooses `keep_trying` (progress is real) / `stop_and_ask`
  (structured help request: how far, what was tried, why stopped, exactly
  what's needed) / `stop` (calibrated). On keep-trying, recovery grafts
  replacement stages transactionally: dependents and reviews rewire to the new
  terminal; the failed node survives only as history; the failed method is
  suppressed unless the planner re-justifies it with a NEW contract.
* **Research delegation** (`request_research`): any agent — stage, developer,
  or repair — that recognizes it lacks know-how asks the orchestrator; a
  bounded research agent (web search + on-demand page fetching) returns a
  findings brief labeled **method knowledge / testimony** (it can never become
  source evidence about the target), cached per topic, budgeted per run.

---

## 8. Bounded autonomy — budgets and ceilings (config.py)

| Judgment decides earlier; ceilings guarantee an end | Default |
| --- | --- |
| Replans on evidence gaps / recoveries on failures | 1 / 5 per run |
| Research requests (shared, cached) / research agent turns | 4 / 10 |
| Persistence decisions / routine supervisor consults | 6 / 12 |
| Agent sessions per run / turns per session / calls per tool | 40 / 24 / 25 |
| Map workers per stage / parallel sessions | 12 / 3 |
| Review rounds / courier fetches / artifact repairs / tool repairs | 2 / 1 / 2 / 2 |
| Sandbox ceilings (declared needs clamped) | 2 GB / 2 CPU / 300 s, no swap |
| Inline images per tool result / image size | 3 / 2 MB |

---

## 9. Safety substrate

* **Isolation**: agent-authored code runs only in labeled, resource-clamped
  sandbox containers; the brain reaches Docker through a whitelisted
  socket-proxy; containers are ledgered, reaped, and leak-checked every run.
* **Locked agent sessions** (`factory.py`): `tools=[]`, allow-listed MCP tools
  only, `permission_mode="dontAsk"`, no host settings, no agent recursion —
  width and depth decisions live in engine code, not in prompts.
* **Approval gate** (`approval.py`): the single interactive trust boundary —
  approvals, hidden credential collection (session-only, name-only tracing),
  visible input collection (e.g. an accessible file path). Free-first: it
  fires only when no free path exists.
* **Redaction**: secret-shaped values are scrubbed at the tracer and sandbox
  boundaries; canary tests byte-scan every disk channel; a pre-commit hook
  blocks secret-shaped diffs.
* **Watchdogs**: per-session stall/total timeouts with dying-session salvage;
  janitor heals leftovers from killed sessions on startup.

---

## 10. Memory & observability

* **Blackboard** (SQLite, per run): objective, plan, stage outputs, evidence
  gaps, method failures, blockers, plan rationale — controller state that
  survives the process.
* **Continuity** (`memory/history.py`): relevant prior runs are summarized for
  the planner; the most relevant prior workspace is seeded under
  `workspace/prior/`; prior conclusions are injected **labeled as hypothesis,
  not evidence** — they steer, they never ground.
* **Negative procedural memory**: capability needs that failed to build are
  recorded and shown to future planners ("re-selecting requires new
  rationale"); quarantined tools are excluded from the catalog.
* **Tracer**: every run is spans + events in SQLite, a tailable JSONL, and a
  human `trace.md`; exportable to Phoenix (OpenTelemetry). Agent outputs,
  prompts, authored tool attempts, and verdict records are first-class files
  under `data/runs/<id>/`.

---

## 11. Known gaps (deliberately open at 0.11.0)

Tracked for the 1.0 line, in `CHANGELOG.md` and the review documents:
uncertainty-driven *active* observation (densify/re-observe where claims
depend on gaps); conflicting-reference adjudication (incompatible user notes
vs. direct evidence, explained rather than silently merged); observe-once
consolidation (typed observation map instead of repeated perception); a
cost/effort governor; deterministic cross-run retry-rationale enforcement.
