# Generality Audit — is the core algorithm general, or shaped by its tasks?

**Date:** 2026-07-19 · **Engine at audit:** v0.25.1 (suite 395) · **Auditor:** owner session

> Not re-run since. The engine is now v0.31.0; changes after v0.25.1 —
> including `side_effects` (a new closed vocabulary) and contract-based
> capability resolution — have not been put through the two audit tests
> in §8. Treat the verdict as accurate for v0.25.1, not as current.

## 1. Purpose and standard

The founding strategy: a system that **learns about the world through
interactions, forms teams to take the appropriate actions based on what it
discovers, and develops the tools it needs.** The binding invariant derived
from it: the core loop — *analyze the task → decompose → identify
requirements → generate the agents and tools needed → execute* — must be
statable with **no task, topic, or tool name**; capabilities are identified
by **contract**, never assumed by name; there are **no fixed pipelines**; and
no engine change may encode a specific task's solution shape.

This audit asks: after ~26 releases driven by concrete tasks (document
transformation, video comprehension, artifact production, spreadsheet
analysis, a credentialed external ML competition), did task-specific
functionality leak into the engine?

## 2. Method

All 13,014 lines of `brain/` were swept:

- **Task-noun sweep** — every task the engine was ever developed against
  contributed nouns (kaggle, titanic, youtube, ebook, ISO-42001, leaderboard,
  passenger, transcript-as-topic, board-pack, helvia/helioroute/kelvane,
  ffmpeg/whisper/SmolVLM, …) grepped case-insensitively across all engine
  code, prompts, and comments.
- **Prompt-literal scan** — all 975 string literals ≥40 chars in the
  orchestrator (the largest prompt surface) programmatically scanned against
  the noun list; planner/factory/workflow/acquisition prompt blocks read.
- **Name-keyed behavior sweep** — every hard-coded capability name in engine
  logic located and traced to its failure mode if the name were absent.
- **Fixed-pipeline check** — search for hard-coded stage ids/sequences.
- **Closed-vocabulary inventory** — every engine-side list that gates
  behavior (extension tables, unit grammars, action enums, evidence tiers).
- **Architecture mapping** — subsystems mapped against the three founding
  clauses (learn / team / tools).

## 3. Verdict

**The core algorithm is general.** No task-specific functionality was found
in engine logic. Concretely:

- **Zero task nouns** in any code path, prompt, contract, or configuration.
  The only hits in the entire tree are two *comments* citing past incidents
  ("the ebook confabulation", "after the transcript yielded nothing") — house
  style records the incident that motivated a general rule; the rules
  themselves are task-free.
- **Zero fixed pipelines.** Stage graphs are planner-emitted per objective;
  the only engine-constructed stage is the planner's degraded-mode fallback
  (one generic research stage when the model emits no plan at all).
- **975 orchestrator prompt literals scanned: no task vocabulary.** Prompt
  modality words (frames, screenshots, charts, rendered pages) are general
  perception categories teaching *decomposition* (acquire → convert signal to
  files → perceive), never a specific task's solution.
- **Every capability need flows through the contract-mediated path**:
  planner declares needs with instance-neutral contracts → resolver decides
  reuse-vs-create against the registry catalog → acquisition authors from the
  contract → the same generic evidence-contract verification battery gates
  promotion for authored tools *and* the bootstrap recipe ("recipes get no
  privileged bypass"). Render records are identified **by shape, not name**
  (`_is_render_record`: consumed a page-layout file, produced images — covers
  any equivalently-contracted tool a plan authors).
- The externally-driven development process held the line: every
  task-provoked failure this campaign (operator credentials, deliverable-vs-
  input resolution, credential-usage contract, independent trials, epoch-aware
  persistence, fail-open auxiliary sessions) shipped as a mechanism statable
  without the task noun, and each is exercised by generic tests.

Task-specific machinery exists **where it belongs, outside the engine**: the
testlab's task adapters (competition submission, external graders, credential
provisioning for a specific task) live in the test repository by design. The
engine repo contains none of it.

## 4. Findings

### Tier A — task contamination: **none found**

### Tier B — name-keyed infrastructure (letter-of-invariant deviations)

Four places where engine code named a capability instead of declaring a
contract. None is task-specific — all serve general activities (observe the
web, perceive an artifact) — but each was a *closed, code-level* binding that
the invariant says should be *declared data*:

| # | Site | What it was | Failure mode if name absent |
|---|------|-------------|------------------------------|
| B1 | `verifier.py` `VERIFIERS = {"web_search": …}` | module-level name-keyed verifier registry (one entry) | fell back to "no verifier registered" error |
| B2 | `orchestrator._research` | research agent toolbox hard-coded inline (`["web_search", "fetch_page_text"]`) + one inline seeded contract | tools authored from contract — path already general, but the *kit* was wired into logic |
| B3 | `_RENDERABLE_EXTS` table | closed extension set gating perceptual review | unlisted format → silently unperceived |
| B4 | `planner` fallback | degraded-mode stage names `web_search` literally | none (registry/recipe covers) — but a literal, not a derivation |

### Tier C — closed vocabularies (deliberate, kept)

- **Supervisor action enums** (`proceed/inform_user/redo_stage/abort`,
  `keep_trying/stop_and_ask/stop`, `continue/stop`) — closed **by design**:
  bounded action vocabularies are the safety property (the model advises;
  engine code executes only valid actions).
- **Evidence tiers** (`target/derived/proxy/retained`) and **observation
  states** — a closed epistemic ontology; openness here would destroy the
  meaning of the certificate.
- **Countable-unit grammar** (sentence/paragraph/word/section/heading/bullet/
  item/line/slide/page) — a general *document-measurement* vocabulary, closed
  so that count criteria stay deterministically measurable (a hard-won
  honesty property). Extending it is future work (§7), not contamination.
- **Bootstrap recipe** (`web_search`, the single pre-built tool) — documented
  scaffolding, verified by the same battery as authored tools, replaceable by
  the authoring path.

### Tier D — architecture vs the founding strategy

| Founding clause | Present subsystems |
|---|---|
| *learns about the world through interactions* | research delegation channel; plan-declared knowledge needs; evidence ledger with lineage-to-fixpoint; information-epoch attempt memory; cross-run capability registry + negative memory (`record_failure`); run-scoped cross-contract failure log; tool health monitoring; independent-trial mode |
| *forms teams to take the appropriate actions* | planner-emitted stage graphs (single/map/reduce/review); role-scoped agents; team channel (ask-upstream, blockers, evidence gaps, tool-defect reports); supervisor judgments at decision boundaries; recovery re-adjudication |
| *develops the tools it needs* | contract-driven authoring in sandboxes; generic evidence-contract verification; promotion/quarantine/revalidation by validation epoch; runtime repair of degraded tools; continuation-judged persistence with epoch grace |

The architecture *is* the founding plan, elaborated. No subsystem exists that
serves one task class only.

## 5. Generalizations implemented (v0.26.0)

The unifying defect class behind Tier B: **innate behavior encoded as code
instead of declared as data.** The fix is one move applied four times — a new
`brain/bootstrap.py` declares the engine's *innate needs as contracts*, and
the loop consumes them like any other need:

1. **`bootstrap.OBSERVATION_NEEDS`** — the observation channel (search +
   page-fetch) declared as `{need_key: contract}`. The research agent's kit
   derives from this data; the inline list and inline contract are gone. A
   missing entry is authored from its contract like any other need (B2).
2. **Renderer perception learns from the registry** —
   `bootstrap.renderable_exts(registry)` = engine default table **∪ the
   promoted renderer's own declared `accepts`**. When the system authors a
   renderer that handles a new format, perceptual review covers that format
   from then on — *the tool the system built extends what the engine
   perceives*, which is the founding clause "learns through interactions"
   made concrete (B3). Render-record detection uses the same learned set.
3. **Recipes carry their own shape check** — the recipe tuple gains its
   verifier; the name-keyed module registry (`VERIFIERS`) is deleted (B1).
4. **Planner fallback derives from the bootstrap data**
   (`primary_observation_need()`), no literal (B4).

## 6. Deliberately unchanged

- Incident-citing comments (house style: the incident is history, the rule is
  general).
- Closed safety enums, evidence ontology, measurement grammar (§4 Tier C).
- The single bootstrap recipe: a system that must observe before it can
  author needs one verified way to observe. It remains data + fallback, not
  a precondition.

## 7. Future evolution (opportunities, not defects)

- **Research kit by contract relevance**: let the research agent additionally
  receive *promoted* tools whose contracts match the topic (kit remains
  data-derived; today it is the fixed observation pair).
- **Declarable countable units**: allow a plan to bind a custom unit
  (e.g. "scene", "endpoint") to a deterministic counting rule, keeping
  measurement honest while opening the vocabulary.
- **Render feasibility by probe**: replace the extension *prior* with an
  attempt-and-report cycle when cost allows.
- **Recipe-as-matched-implementation**: resolve recipes by contract
  similarity rather than key equality (moot while one recipe exists).

## 8. Standing rule for future development

Before merging any engine change, apply the two audit tests used here:

1. **The statement test** — the change must be statable with no task, topic,
   or tool name (existing law).
2. **The vocabulary test** *(new)* — any list/name the change adds to engine
   code is either (a) a closed safety/ontology enum with a stated reason, or
   (b) *declared data the run can extend* (a contract, a registry-derived
   set). A name wired into logic is the defect class this audit exists to
   catch.
