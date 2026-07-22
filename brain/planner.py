"""Planner: objective -> a staged workflow.

A locked-down LLM session designs the *workflow* for the objective — a DAG of
stages, each with an execution pattern (single | map | reduce | review) — and
emits it through the structured ``emit_plan`` tool. The planner chooses the
topology per task (a simple question stays one stage; an evidence-heavy task
becomes gather -> map -> reduce -> review), guided by an explicit effort rubric
rather than vibes. The orchestrator decides a map stage's actual width at run
time from the manifest its upstream stage produced.

``StubPlanner`` returns a fixed workflow for deterministic tests.
"""
from __future__ import annotations

import json
from typing import Optional

from .models import Plan, StageSpec
from .workflow import normalize_stages

_SYSTEM = (
    "You are the workflow designer of a self-organizing multi-agent system. Design the "
    "SMALLEST staged workflow that genuinely accomplishes the user's objective.\n"
    "\n"
    "First judge the objective's scale and depth, then pick the shape:\n"
    "- Focused task one agent can hold in its head end-to-end => ONE `single` stage "
    "(add a second only for a truly distinct skill). Never add ceremony to simple tasks.\n"
    "- Work spread over MANY items (sources, documents, files, records...) that exceeds "
    "one agent's attention => a `single` gather stage whose goal ends by ENUMERATING the "
    "items found, then a `map` stage whose workers each deeply process a few items, then "
    "a `reduce` stage that consolidates the workers' notes into one deliverable.\n"
    "- Deliverable whose factual accuracy matters (claims, numbers, citations) => append a "
    "`review` stage that verifies the draft against the gathered evidence.\n"
    "- The more the information is TRANSFORMED before delivery (e.g. source -> extracted text -> "
    "analysis -> distilled points -> visual artifact), the more distortion can accumulate and the "
    "more a `review` stage is warranted — scale review to that risk, not to task size.\n"
    "\n"
    "Stage fields: `id` (short slug), `pattern`, `role`, `goal` (one sentence), "
    "`capabilities`, `depends_on` (stage ids), plus for map: `shard_size` (items per "
    "worker, default 5) and `shard_from` (which dependency's item list to divide); for "
    "review: `rounds` (verdict rounds — a budget of N allows N-1 fix cycles, so "
    "the engine enforces a minimum of 2; omit it unless you need more). For any "
    "OBSERVATION stage, also give `expectation` "
    "— what a SUCCESSFUL run should yield (e.g. 'full-duration coverage of the visual "
    "content', 'the ordered event sequence', 'N records'). The engine compares this to what "
    "was actually observed; the gap tells it whether a retry is making progress or has "
    "plateaued, so a good expectation makes the system persist intelligently. "
    "When the objective asks for a concrete deliverable (a document, artifact, dataset, "
    "program), set `deliverable: true` on the ONE stage that PRODUCES it — never on an "
    "inspection or verification stage. That stage's output is what ships to the user; "
    "leaving it unmarked lets deliverable ownership drift to whatever stage runs last.\n"
    "Pattern semantics:\n"
    "- single: one agent, runs once.\n"
    "- map: you describe ONE worker; the system replicates it at run time over the items "
    "the `shard_from` stage actually found. Write role/goal for a single worker working "
    "on ITS OWN few items only (it never sees the rest).\n"
    "- reduce: one agent that receives all workers' notes and merges them into the "
    "deliverable. Needs no capabilities for reading the notes.\n"
    "- review: one agent that receives the draft AND the underlying notes, checks every "
    "load-bearing claim against them (flag anything unsupported or from a weak source), "
    "and either approves or demands specific fixes.\n"
    "\n"
    "Capabilities are EXTERNAL tools an agent runs in a sandbox — e.g. searching the web, "
    "fetching/parsing/generating files, or running code — things a plain LLM cannot do by "
    "itself. Writing, summarizing, analyzing, comparing, synthesizing, recommending, "
    "reasoning, and PERCEIVING files the run already holds — reading their text AND LOOKING "
    "AT images (frames, screenshots, scans, rendered pages, charts, photos) — are an agent's "
    "OWN abilities, NOT capabilities: the reasoning model is natively multimodal and opens "
    "retained files with the always-available read_file tool. Such stages have an EMPTY "
    "`capabilities` list. Therefore do NOT author an image-captioning / VLM / OCR / "
    "describe-image / vision-model tool — that rebuilds sight the agent already has and will "
    "blow the sandbox's memory/compute ceiling. Author instead the CHEAP tool that PRODUCES "
    "the files to look at (a downloader, a converter, a frame/segment/page extractor that "
    "writes PNG/JPEG/text), then give the images to an ordinary agent stage that describes "
    "what it sees.\n"
    "When the user PROVIDES FILES (listed in the objective as workspace paths), design stages "
    "that actually OPEN and process them via file-handling capabilities — agents pass the "
    "listed paths through a tool's `files` argument; never assume a file's contents. A format "
    "the system has no tool for means naming a NEW capability for it. Treat verification as "
    "part of the work: whoever reads a file should check the extraction is complete and "
    "faithful (counts, structure, non-text content such as images) and surface anything that "
    "needs additional processing rather than silently ignoring it.\n"
    "EVIDENCE BEFORE MECHANISM: first identify what must be OBSERVED for the deliverable's "
    "load-bearing claims — identity? full content? ordered events? structure? state? — then "
    "bind stages to capabilities that can actually observe THOSE properties. An available "
    "capability is not the epistemic target: retrieving metadata about a source is not "
    "observing its content, and a familiar mechanism must never substitute for the method "
    "the objective actually requires. A capability's description may carry residue of the "
    "task that created it — judge fit by its input/output contract, not its narrative. If "
    "no capability can observe a required property, NAME A NEW capability for that "
    "observation (the system builds it) rather than binding a near-miss.\n"
    "NEVER design a stage or capability that asks the user for input, credentials, keys, or "
    "configuration — sandboxed tools cannot reach the user. When a tool genuinely needs a "
    "credential (API key, bot token, connection string), the ENGINE detects it and collects "
    "the value from the user interactively, automatically. Just design the stage that USES "
    "the external service; if the user asked to be told how to obtain a credential, a stage's "
    "agent may include that explanation in its OWN text output.\n"
    "Existing capabilities you may REUSE, but ONLY when the description genuinely matches "
    "the need:\n"
    "{known}\n"
    "For any other concrete external action, INVENT a new, SPECIFIC snake_case capability "
    "key that names exactly what it does (e.g. generate_pdf, fetch_page_text, "
    "embed_and_search). Do NOT reuse an existing key whose description does not fit, and "
    "avoid vague umbrella names like 'code_execution'. The system will build any new "
    "capability you name.\n"
    "ATOMIC CAPABILITIES: one operation per capability — an observer observes, a "
    "transformer transforms, a synthesizer synthesizes, a validator validates. Never a "
    "fetch-and-summarize monolith: when the optional interpretation half fails, it destroys "
    "the successful observation half with it, and the compiled-in task policy makes the "
    "tool unusable for any other objective. Interpretation and final formatting belong to "
    "the STAGE AGENT, which composes atomic tools.\n"
    "CAPABILITY NEEDS: FIRST check the known-capabilities catalog — when an existing "
    "tool's CONTRACT covers a need, you MUST reuse its exact name rather than declaring "
    "a synonym (a run was observed authoring a second page renderer while the first sat "
    "promoted and in use; every duplicate build burns budget and splits provenance). "
    "Only for genuinely NEW capability keys, add an entry to "
    "`capability_needs` with: `key`; `contract` — an INSTANCE-NEUTRAL statement of the one "
    "operation (what it observes/does, its inputs, its outputs, and that partial/negative "
    "yield must be reported honestly) containing NO specific URLs/paths/identifiers, NO "
    "output-shape policy (counts, paragraph limits), and NO final-answer synthesis; and "
    "`example_input` — the CONCRETE values from THIS objective (the actual locator, query, "
    "identifier) used only to verify the built tool; for a FILE-CONSUMING capability, "
    "include `files`: the workspace-relative paths of the real provided files (e.g. "
    "under input/) — the engine mounts them so the tool is verified against the actual "
    "input, which is the only way to verify binary formats. The builder sees the "
    "contract, not your stage goals — a contract that smuggles task policy poisons the "
    "reusable library.\n"
    "STRATEGY ADJUDICATION: when the objective depends on observing a designated external "
    "source/system (or the method choice is otherwise consequential), reason in "
    "`strategy_rationale` in this order:\n"
    "1. TARGET SIGNAL MODEL — state what the designated target IS and enumerate its "
    "constituent observable signals: structure, stored content, temporal/visual/auditory "
    "channels, runtime behavior, state, metadata. Say which signal CARRIES each property "
    "the deliverable needs. The property lives in the target's own signals; convenience "
    "channels (captions, indexes, docs, summaries) are derivatives of them.\n"
    "2. CANDIDATE METHODS — the comparison MUST include (a) DIRECT OBSERVATION of the "
    "designated target itself, and (b) a PROPERTY-PRESERVING DERIVED REPRESENTATION. "
    "DECOMPOSE observation into acquirable sub-steps — ACQUIRE the object (download / open / "
    "connect) → CONVERT its carrying signal into files (extract frames, segment audio, "
    "render pages, dump state, export rows) → PERCEIVE those files (an agent reads text and "
    "LOOKS AT images natively). Each sub-step is either a capability YOU AUTHOR at runtime "
    "or the agent's own ability; the system builds any tool you name, so name the specific "
    "atomic tools the pipeline needs (e.g. a downloader, a frame extractor) in "
    "`capability_needs`. THE ABSENCE OF AN EXISTING TOOL IS NEVER A BLOCKER — if the tool "
    "does not exist yet, NAME IT and the system builds it; you must NOT downgrade to a "
    "weaker proxy (search snippets, third-party write-ups, metadata) merely because the "
    "direct tool is not already installed. A proxy is admissible only as a SUPPLEMENT to "
    "direct observation, or when direct observation is truly infeasible for one of the "
    "reasons below. Reject direct observation ONLY for a genuine, intrinsic blocker, and "
    "record it in `direct_observation` (see below) with a `blocker_class` of: "
    "requires_credential (needs an account/key only the user can provide), resource_ceiling "
    "(needs compute beyond the sandbox's memory/CPU/time ceiling AND no lighter variant "
    "exists), no_free_means (no free, keyless software or library can acquire or convert the "
    "signal), physical_access (needs real-world or interactive access the sandbox cannot "
    "reach), or user_authority (needs permission only the user can grant). 'No tool is "
    "installed / available / ready' is NOT a blocker_class and never justifies a proxy. "
    "A blocker_class must be justified by THIS run, not by a prior run's failure — a past "
    "failure may have had a different cause or been fixed since. resource_ceiling in "
    "particular applies ONLY to loading a heavy model or an entire large payload into memory "
    "at once; ACQUIRING and CONVERTING a signal (downloading, extracting, sampling, "
    "streaming, rendering page-by-page) and PERCEIVING the resulting files are NOT "
    "ceiling-bound — perception is the agent's own ability and needs no model, so never "
    "claim resource_ceiling for extraction or perception. Silent omission of direct "
    "observation is a planning defect.\n"
    "3. TIER GRADING — grade every candidate by evidence tier: direct observation > "
    "property-preserving derived representation > primary-source testimony > search-index "
    "snippets > secondary testimony > memory/inference. Choose by EPISTEMIC FIT FIRST, "
    "then cost. A lower tier may win only with explicit justification, and the deliverable "
    "must carry that limitation. Snippets and pages ABOUT a target are testimony — never "
    "call them direct observation of it.\n"
    "4. BELIEF HYGIENE — prior-run conclusions and world knowledge about the target are "
    "HYPOTHESES: they may order which channels to try first, but must never RULE OUT an "
    "observation channel ('the target has no X') unless that absence was verified THIS "
    "run. Always emit `strategy_rationale` (it is persisted and audited). Prefer a plan "
    "whose first method failing still leaves the alternatives reachable — observation "
    "before synthesis, not fused into it.\n"
    "Also compile `success_criteria`: a short list of CHECKABLE conditions the finished work "
    "must meet, derived from THIS objective's explicit demands — counts ('exactly 3 sections'), "
    "formats ('self-contained HTML'), fidelity ('every claim traceable to the provided file'), "
    "constraints ('under 40 words'), and self-consistency ('the final answer accurately "
    "describes the artifact produced'). Fidelity criteria must bind to the USER'S ORIGINAL "
    "source/evidence, never to an intermediate stage's notes — 'traceable to the analysis' "
    "permits a perfectly analysis-consistent fabrication — and never to YOUR CHOSEN "
    "MECHANISM: 'traceable to the <channel you planned to use>' freezes a method "
    "assumption that becomes incompatible with the objective the moment that channel "
    "yields nothing; write 'traceable to observations of the source' instead. Derive them from the request; never "
    "emit a fixed template, and NEVER a criterion about the review/approval process itself "
    "('the review approves…' is circular — criteria bind the DELIVERABLE to observations, "
    "not the process to its own verdict). SEMANTIC DECOMPOSITION: identify which DIMENSIONS "
    "of understanding the request actually asks for — entities/events (what happened), "
    "mechanism/causes (how/why), topic/theme (what it is ABOUT, its governing idea), "
    "evaluation, or chronology — and compile a criterion for EACH requested dimension: an "
    "accurate event inventory does not satisfy a request to explain subject or meaning. "
    "Where a dimension requires INTERPRETATION beyond direct observation, the criterion "
    "must demand the deliverable present it as interpretation grounded in cited "
    "observations, distinct from the observations themselves. A reviewer will check the "
    "deliverable against them, and countable constraints (paragraphs, words, sections) are "
    "also MEASURED by the engine against the exact deliverable payload.\n"
    "KNOWLEDGE NEEDS — DEFINE GOOD BEFORE BUILDING: when the deliverable's FORM is "
    "judged by an audience (a presentation, report, page, dashboard, interface, "
    "publication-grade figure — any artifact whose quality depends on domain conventions "
    "the objective does not spell out), you must either (a) encode the applicable "
    "conventions directly as success_criteria you are CONFIDENT of, or (b) emit "
    "`knowledge_needs` — the standards to research first ({{topic, why}}), e.g. the layout, "
    "structure, and data-graphic conventions of the deliverable's genre. The engine "
    "researches them and re-invokes you ONCE with the findings; your final criteria must "
    "then encode the researched standards as CHECKABLE conditions (layout integrity — no "
    "overflow/clipping; correct data-graphic encoding — honest axes, one encoding per "
    "series; consistency; no editorial meta-text in audience-facing copy). Mediocrity is "
    "a failure mode: a deliverable can be grounded and still amateur — the quality bar "
    "must be DEFINED, never assumed.\n"
    "ASPIRATION — the standing goal: when the objective states a MEASURABLE aspiration "
    "whose TRUE value is observed from the world (a score returned by an external "
    "evaluator, a latency, a rate, a ranking), declare `aspiration`: metric (what is "
    "measured), direction (maximize|minimize|reach), target if the user stated one, and "
    "measured_by (HOW the true value is observed — internal estimates are never it). The "
    "run's outcome is judged against it at run end and it PERSISTS as a standing goal "
    "across runs. If a STANDING GOALS block (in context) lists an open goal this "
    "objective continues, set aspiration.goal_ref to its id instead of redeclaring. If — "
    "and ONLY if — the user's own words grant an iteration budget ('pursue this, up to N "
    "attempts', 'keep iterating, budget $X'), set budget_attempts / budget_cost_usd; "
    "NEVER invent a budget, and never declare an aspiration for a one-shot deliverable "
    "with no stated measurable outcome.\n"
    "REVISION MODE — session continuity: when SESSION CONTEXT (below, if present) shows a "
    "previous run's deliverable and the new input reads as FEEDBACK on it (dissatisfaction, "
    "change requests, 'make it deeper/cleaner/shorter', complaints about specific parts), "
    "plan a REVISION, not a rebuild: set `revision` {{of_run, feedback — what the user is "
    "dissatisfied with, in your words; preserve — what already satisfied them}}. Design "
    "stages that LOAD the prior deliverable and its retained materials (seeded under "
    "prior/<run_id>/ in the workspace) and change ONLY what the feedback targets; "
    "re-extracting or re-deriving what already exists wastes the user's time and risks "
    "regressing approved content. SCOPE OF A CHANGED FACT: the feedback's targets "
    "include every statement whose truth DEPENDS on a changed fact — figures derived "
    "from it, comparisons built on it, restatements of it in other answers/sections — "
    "not only the text that quotes it; encode that propagation in the criteria. Carry the ORIGINAL objective's demands forward as "
    "success_criteria — they still bind — and ADD criteria encoding the feedback. If the "
    "new input is an unrelated task, ignore revision and plan normally.\n"
    "You MUST call `emit_plan` exactly once with the full workflow and must NOT answer in prose."
)

_EXTEND_ADDENDUM = (
    "EXTENSION MODE: a run of this workflow is ALREADY EXECUTING and has reported "
    "specific observation needs its current methods cannot establish. Do NOT re-plan "
    "completed work. Emit ONLY the additional stages that observe what is missing. "
    "New stages may depend on the existing stage ids provided in the prompt. If one of "
    "your new stages now PRODUCES the user-requested deliverable (replacing a failed "
    "producer), mark it `deliverable: true` — inspection/verification stages never are.\n"
    "FAILED METHODS ARE EVIDENCE: a need may arise because a method FAILED — its tool "
    "could not be built, or its stage died (the classified failure account is included "
    "with the need). Treat that as an observation about the strategy: do NOT re-select "
    "the failed method unless the failure account itself justifies a targeted variation "
    "(state that justification in `strategy_rationale`); prefer a materially different, "
    "source-aligned method, and carry forward any retained partial observations instead "
    "of re-establishing them. If NO admissible method remains — or the only remaining "
    "routes need authority/input only the user can grant — emit an EMPTY stages list: "
    "a calibrated stop that the run reports honestly beats a doomed rebuild.\n"
    "STRATEGY ADJUDICATION (required): before choosing, enumerate the materially "
    "different ways the missing property could be established — direct observation of "
    "the designated target; a transformation that PRESERVES the needed property; an "
    "authoritative first-party description; independent secondary descriptions (a "
    "PROXY: evidence of what others say, not of the target itself); or honest "
    "abstention. Choose by EPISTEMIC FIT FIRST — how directly and reliably the method "
    "observes the requested property — then cost. A cheap, familiar method that cannot "
    "observe the property must not win over a feasible source-aligned one; if you "
    "select a proxy, state why more direct routes are infeasible or disproportionate. "
    "Put this comparison in `strategy_rationale` (it is traced and audited). If no "
    "current capability can observe the required property, name a NEW capability for "
    "it — the system builds tools at runtime; do not settle for a near-miss.\n"
    "CRITERIA RECOMPILATION: if an existing success criterion encodes the FAILED "
    "mechanism (e.g. binds claims to a channel that yielded nothing), emit a revised "
    "`success_criteria` list — preserve every demand of the user's objective and its "
    "explicit output constraints; only the method-bound assumptions may change, and "
    "claims established via a proxy must be required to CARRY that limitation."
)

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "stages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "pattern": {"type": "string", "enum": ["single", "map", "reduce", "review"]},
                    "role": {"type": "string"},
                    "goal": {"type": "string"},
                    "capabilities": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "shard_size": {"type": "integer"},
                    "shard_from": {"type": "string"},
                    "rounds": {"type": "integer"},
                    "expectation": {"type": "string",
                                    "description": "what a SUCCESSFUL run of this stage should "
                                                   "yield (coverage, count, the property observed)"},
                    "deliverable": {"type": "boolean",
                                    "description": "true on the ONE stage whose output IS the "
                                                   "user-requested deliverable (the producer — "
                                                   "never an inspection/verification stage)"},
                },
                "required": ["id", "pattern", "role", "goal"],
            },
        },
        "success_criteria": {"type": "array", "items": {"type": "string"}},
        # The comparison of candidate methods and why the chosen one best
        # observes the required properties (persisted and audited).
        "strategy_rationale": {"type": "string"},
        # Accountable decision on observing the designated source. `planned`
        # (a stage in this plan observes it directly / via a property-preserving
        # representation, authoring the tool if needed) is the default for any
        # source-derived objective; `infeasible` is admissible ONLY with a
        # blocker_class from the closed enum — "no tool exists" is not one, so
        # tool-absence can never justify skipping direct observation.
        "direct_observation": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["planned", "infeasible"]},
                "capability": {"type": "string",
                               "description": "the stage/capability that observes the source"},
                "blocker_class": {"type": "string", "enum": [
                    "requires_credential", "resource_ceiling", "no_free_means",
                    "physical_access", "user_authority"]},
                "explanation": {"type": "string"},
            },
        },
        # Instance-neutral authoring contract per NEW capability key; the
        # concrete task values ride separately as example_input (verification
        # only) so task policy never compiles into reusable mechanism code.
        "capability_needs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "contract": {"type": "string"},
                    "example_input": {"type": "object"},
                },
                "required": ["key", "contract"],
            },
        },
        # Knowledge the plan itself lacks: domain standards/conventions that
        # define what a GOOD deliverable looks like, researched BEFORE building.
        "knowledge_needs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "why": {"type": "string",
                            "description": "which decision or quality bar it informs"},
                },
                "required": ["topic"],
            },
        },
        # Set when this plan REVISES the previous run's deliverable in
        # response to user feedback (session continuity).
        "revision": {
            "type": "object",
            "properties": {
                "of_run": {"type": "string"},
                "feedback": {"type": "string",
                             "description": "what the user was dissatisfied with"},
                "preserve": {"type": "string",
                             "description": "what already satisfied the user and must survive"},
            },
        },
        # Measurable aspiration the objective states (standing goal), if any.
        "aspiration": {
            "type": "object",
            "properties": {
                "goal_ref": {"type": ["integer", "string"],
                             "description": "id of the open standing goal this continues"},
                "metric": {"type": "string"},
                "direction": {"type": "string", "enum": ["maximize", "minimize", "reach"]},
                "target": {"type": ["string", "number"]},
                "measured_by": {"type": "string",
                                "description": "how the TRUE value is observed from the world"},
                "budget_attempts": {"type": "integer",
                                    "description": "ONLY if the user granted one"},
                "budget_cost_usd": {"type": "number",
                                    "description": "ONLY if the user granted one"},
            },
        },
    },
    "required": ["stages"],
}


def _fallback_stages(objective: str) -> list[StageSpec]:
    """Degraded mode (the model emitted no workflow at all): one generic
    research stage whose single capability derives from the engine's declared
    innate observation needs — data, not a name wired into logic."""
    from . import bootstrap

    return [StageSpec(id="researcher", pattern="single", role="researcher",
                      goal=objective,
                      capabilities=[bootstrap.primary_observation_need()])]


def _normalize_aspiration(raw) -> dict:
    """Coerce the emitted aspiration; {} when none was meaningfully declared.
    A budget survives ONLY as a positive number — the planner is instructed
    never to invent one, and garbage must not authorize autonomous iteration."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict = {}
    if raw.get("goal_ref") not in (None, ""):
        out["goal_ref"] = str(raw["goal_ref"])[:20]
    metric = str(raw.get("metric") or "").strip()
    if metric:
        out["metric"] = metric[:400]
        direction = str(raw.get("direction") or "maximize").strip().lower()
        out["direction"] = direction if direction in ("maximize", "minimize", "reach") \
            else "maximize"
        out["target"] = str(raw.get("target") or "")[:100]
        out["measured_by"] = str(raw.get("measured_by") or "")[:400]
    for key in ("budget_attempts", "budget_cost_usd"):
        try:
            v = float(raw.get(key))
            if v > 0:
                out[key] = int(v) if key == "budget_attempts" else v
        except (TypeError, ValueError):
            pass
    return out if ("metric" in out or "goal_ref" in out) else {}


def _normalize_knowledge_needs(raw) -> list[dict]:
    out = []
    for n in raw or []:
        if isinstance(n, dict) and str(n.get("topic", "")).strip():
            out.append({"topic": str(n["topic"])[:300], "why": str(n.get("why", ""))[:300]})
    return out[:6]


def _normalize_revision(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    if not str(raw.get("feedback", "")).strip():
        return {}
    return {"of_run": str(raw.get("of_run", ""))[:120],
            "feedback": str(raw.get("feedback", ""))[:500],
            "preserve": str(raw.get("preserve", ""))[:500]}


_LEGIT_BLOCKERS = {"requires_credential", "resource_ceiling", "no_free_means",
                   "physical_access", "user_authority"}


def _normalize_direct_observation(raw) -> dict:
    """Coerce the planner's direct-observation declaration into a stable shape.
    A missing/garbled declaration defaults to 'unstated' so the source-derived
    guard can catch it — silence is not consent to a proxy."""
    raw = raw if isinstance(raw, dict) else {}
    decision = str(raw.get("decision") or "unstated").strip().lower()
    if decision not in ("planned", "infeasible"):
        decision = "unstated"
    blocker = str(raw.get("blocker_class") or "").strip().lower()
    if blocker not in _LEGIT_BLOCKERS:
        blocker = ""
    return {"decision": decision, "blocker_class": blocker,
            "capability": str(raw.get("capability") or "")[:120],
            "explanation": str(raw.get("explanation") or "")[:600]}


def observation_is_accountable(direct: dict) -> bool:
    """True when the plan either observes the designated source directly or
    names a LEGITIMATE intrinsic blocker. False = a proxy-first plan that
    skipped direct observation without a real reason (tool-absence is not one),
    which the controller must correct."""
    d = direct or {}
    if d.get("decision") == "planned":
        return True
    if d.get("decision") == "infeasible" and d.get("blocker_class") in _LEGIT_BLOCKERS:
        return True
    return False


def _normalize_needs(raw) -> dict[str, dict]:
    """Coerce emitted capability_needs into {key: {contract, example_input}}."""
    out: dict[str, dict] = {}
    for n in raw or []:
        if isinstance(n, dict) and n.get("key") and n.get("contract"):
            ex = n.get("example_input")
            out[str(n["key"])] = {
                "contract": str(n["contract"])[:1200],
                "example_input": ex if isinstance(ex, dict) else {},
            }
    return out


# The top-level parameters emit_plan recognizes — the repair/feedback loop
# below is keyed on these, so extend it when the schema grows.
_PLAN_KEYS = ("stages", "success_criteria", "strategy_rationale",
              "capability_needs", "direct_observation", "knowledge_needs",
              "revision", "aspiration")


def _unwrap_plan_args(args) -> tuple[dict, str]:
    """Deterministic repair of a plan-emission argument envelope.

    A model sometimes wraps the real payload under a single placeholder key
    (observed: three calls carrying everything inside a literal
    '$PARAMETER_NAME' argument, each answered 'plan recorded' — positive
    feedback for an empty capture, then death by turn budget). When no
    recognized parameter is present but a lone envelope value (dict, or a
    JSON-parsable string) contains recognized keys, recover it. Returns
    ``(recognized_args, repair_note)``; empty dict = nothing recognizable."""
    args = dict(args or {})
    if any(k in args for k in _PLAN_KEYS):
        return args, ""
    if len(args) == 1:
        (key, val), = args.items()
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except ValueError:
                val = None
        if isinstance(val, dict) and any(k in val for k in _PLAN_KEYS):
            return val, f"payload unwrapped from envelope key {str(key)[:60]!r}"
    return {}, ""


_EMIT_REJECTION = (
    "NOT RECORDED: no recognized parameter. Call emit_plan again with the real "
    "parameter names at the TOP LEVEL of the tool arguments — "
    f"{', '.join(_PLAN_KEYS)} — not wrapped inside a placeholder key."
)


def _make_emit_plan(captured: dict, tracer, name: str):
    """The emit_plan tool body: capture EVERYTHING the model emitted —
    cherry-picking keys here is how success_criteria got silently dropped
    (#12) — but never return success for a capture of NOTHING; a malformed
    call must get corrective feedback, not 'plan recorded'."""
    async def emit_plan(args):
        fixed, note = _unwrap_plan_args(args)
        if not fixed:
            tracer.event("plan.args_rejected", name, status="failed",
                         keys=[str(k)[:60] for k in (args or {})][:8])
            return {"content": [{"type": "text", "text": _EMIT_REJECTION}],
                    "is_error": True}
        if note:
            tracer.event("plan.args_repaired", name, status="info", note=note)
        captured.update(fixed)
        return {"content": [{"type": "text", "text": "plan recorded"}]}
    return emit_plan


class Planner:
    def __init__(
        self,
        *,
        model: Optional[str] = None,
        known_capabilities=None,
        tracer=None,
    ):
        self.model = model
        kc = known_capabilities or {}
        # Accept {name: description} or a bare iterable of names.
        self.known = dict(kc) if isinstance(kc, dict) else {str(k): "" for k in kc}
        from .observability import NullTracer

        self.tracer = tracer or NullTracer()

    async def plan(self, objective: str, history: Optional[str] = None,
                   corrective: Optional[str] = None,
                   session_context: Optional[str] = None) -> Plan:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        from .factory import locked_session
        from .observability import trace_query

        captured: dict = {}

        emit_plan = tool("emit_plan", "Emit the staged workflow as structured data.",
                         _PLAN_SCHEMA)(_make_emit_plan(captured, self.tracer, "planner"))

        known_str = (
            "\n".join(f"  - {k}: {v}" for k, v in self.known.items())
            if self.known else "  (none yet)"
        )
        server = create_sdk_mcp_server(name="planner", version="1.0.0", tools=[emit_plan])
        options = locked_session(
            system_prompt=_SYSTEM.format(known=known_str),
            mcp_servers={"planner": server},
            allowed_tools=["mcp__planner__emit_plan"],
            model=self.model,
            max_turns=6,
        )

        prompt = objective
        if session_context:
            prompt += (
                "\n\nSESSION CONTEXT (this terminal session's previous task and its "
                "deliverable — judge whether the new input above is FEEDBACK on it; "
                "see REVISION MODE):\n" + session_context
            )
        if history:
            prompt += (
                "\n\nPRIOR WORK by this system (you may design stages that build on it; its files "
                "can be seeded into the current workspace, and its capabilities are reusable):\n"
                + history
            )
        if corrective:
            prompt += "\n\n" + corrective
        try:
            await trace_query(self.tracer, "planner", prompt, options)
        except Exception as e:  # noqa: BLE001 — salvage below or re-raise
            # A VALIDLY EMITTED plan survives its session's death (observed: a
            # turn-budget exception discarded a captured plan and the whole
            # run failed with zero stages executed). The text-salvage path in
            # trace_query cannot cover tool-captured payloads — this does.
            if not normalize_stages(captured.get("stages", []))[0]:
                raise
            self.tracer.event("plan.salvaged", "planner", status="failed",
                              error=f"{type(e).__name__}: {str(e)[:200]}",
                              note="session died after a valid plan was emitted "
                                   "— proceeding with the captured plan")

        stages, warnings = normalize_stages(captured.get("stages", []))
        for w in warnings:
            self.tracer.event("plan.normalized", "planner", status="info", warning=w)
        if not stages:
            stages = _fallback_stages(objective)
        criteria = [str(c) for c in (captured.get("success_criteria") or []) if str(c).strip()]
        # The initial method choice must be as auditable as a replan: an
        # unrecorded rationale is where availability quietly becomes the rule.
        rationale = str(captured.get("strategy_rationale") or "").strip()
        self.tracer.event("plan.strategy", "planner",
                          status="info" if rationale else "failed",
                          rationale=rationale[:1500] or "(none recorded — availability "
                          "may have functioned as the implicit rule)")
        direct = _normalize_direct_observation(captured.get("direct_observation"))
        self.tracer.event("plan.direct_observation", "planner", status="info", **direct)
        return Plan(objective=objective, stages=stages, success_criteria=criteria,
                    strategy_rationale=rationale,
                    capability_needs=_normalize_needs(captured.get("capability_needs")),
                    direct_observation=direct,
                    knowledge_needs=_normalize_knowledge_needs(captured.get("knowledge_needs")),
                    revision=_normalize_revision(captured.get("revision")),
                    aspiration=_normalize_aspiration(captured.get("aspiration")))

    async def extend(self, objective: str, completed: str, gaps: list[dict],
                     existing_ids: list[str]):
        """Re-enter the core loop mid-run: design ONLY the additional stages
        needed to observe what execution revealed it cannot establish — whether
        the trigger was an agent-reported evidence gap or a METHOD FAILURE the
        controller converted into a residual need. The unmet need drives the
        method — the planner may reuse capabilities or NAME NEW ONES for the
        system to author, exactly as in initial planning. Returns
        ``(stages, success_criteria, capability_needs)``; empty stages means no
        additional observation is feasible (a calibrated stop)."""
        from claude_agent_sdk import create_sdk_mcp_server, tool

        from .factory import locked_session
        from .observability import trace_query

        captured: dict = {}

        emit_plan = tool("emit_plan", "Emit the ADDITIONAL stages as structured data.",
                         _PLAN_SCHEMA)(_make_emit_plan(captured, self.tracer,
                                                       "planner.extend"))

        known_str = ("\n".join(f"  - {k}: {v}" for k, v in self.known.items())
                     if self.known else "  (none yet)")
        server = create_sdk_mcp_server(name="planner", version="1.0.0", tools=[emit_plan])
        options = locked_session(
            system_prompt=_SYSTEM.format(known=known_str) + "\n\n" + _EXTEND_ADDENDUM,
            mcp_servers={"planner": server},
            allowed_tools=["mcp__planner__emit_plan"],
            model=self.model,
            max_turns=6,
        )
        need_lines = "\n".join(
            f"- NEED: {g.get('need', '')}" + (f" (attempted: {g['attempted']})"
                                              if g.get("attempted") else "")
            for g in gaps)
        prompt = (
            f"OBJECTIVE (unchanged): {objective}\n\n"
            f"WORK COMPLETED SO FAR (its outputs are available to new stages as "
            f"dependencies):\n{completed}\n\n"
            f"UNMET OBSERVATION NEEDS reported during execution — the objective "
            f"depends on these and current methods could not establish them:\n{need_lines}\n\n"
            f"Existing stage ids (your new stages may depend on them; do NOT reuse "
            f"these ids): {existing_ids}\n\n"
            "Design ONLY the additional stage(s) that can actually observe what is "
            "missing — a different observation method, a new capability to author, or "
            "independent corroborating sources. If NO additional observation is "
            "feasible, emit an empty stages list."
        )
        try:
            await trace_query(self.tracer, "planner.extend", prompt, options)
        except Exception as e:  # noqa: BLE001 — salvage a captured extension;
            # an EMPTY extension is a valid calibrated stop, so only a capture
            # with real stages justifies swallowing the session error.
            if not normalize_stages(captured.get("stages", []) or [])[0]:
                raise
            self.tracer.event("plan.salvaged", "planner.extend", status="failed",
                              error=f"{type(e).__name__}: {str(e)[:200]}",
                              note="session died after a valid extension was "
                                   "emitted — proceeding with the captured stages")
        raw = captured.get("stages", []) or []
        stages, warnings = normalize_stages(raw)
        for w in warnings:
            self.tracer.event("plan.normalized", "planner.extend", status="info", warning=w)
        rationale = str(captured.get("strategy_rationale") or "").strip()
        self.tracer.event("plan.strategy", "planner.extend",
                          status="info" if rationale else "failed",
                          rationale=rationale[:1500] or "(none recorded — availability "
                          "may have functioned as the implicit rule)")
        # normalize_stages drops deps outside ITS list — restore references to
        # already-executed stages (that linkage is the point of an extension).
        raw_deps = {str(r.get("id")): [str(d) for d in (r.get("depends_on") or [])]
                    for r in raw if isinstance(r, dict)}
        known = set(existing_ids)
        for s in stages:
            for d in raw_deps.get(s.id, []):
                if d in known and d not in s.depends_on:
                    s.depends_on.append(d)
        criteria = [str(c) for c in (captured.get("success_criteria") or []) if str(c).strip()]
        return ([s for s in stages if s.id not in known], criteria,
                _normalize_needs(captured.get("capability_needs")))


class StubPlanner:
    """Deterministic planner for tests; returns the workflow it was given."""

    def __init__(self, stages: list[StageSpec], success_criteria: Optional[list[str]] = None,
                 strategy_rationale: str = "", capability_needs: Optional[dict] = None,
                 direct_observation: Optional[dict] = None,
                 knowledge_needs: Optional[list] = None,
                 revision: Optional[dict] = None,
                 aspiration: Optional[dict] = None):
        self._stages = stages
        self._criteria = success_criteria or []
        self._rationale = strategy_rationale
        self._needs = dict(capability_needs or {})
        self._direct = dict(direct_observation or {})
        self._knowledge = list(knowledge_needs or [])
        self._revision = dict(revision or {})
        self._aspiration = dict(aspiration or {})
        self.plan_calls = 0
        self.correctives: list[str] = []
        self.session_contexts: list[str] = []

    async def plan(self, objective: str, history: Optional[str] = None,
                   corrective: Optional[str] = None,
                   session_context: Optional[str] = None) -> Plan:
        self.plan_calls += 1
        if corrective:
            self.correctives.append(corrective)
        if session_context:
            self.session_contexts.append(session_context)
        # Knowledge needs are one-shot: the informed replan (corrective set)
        # must not redeclare them, mirroring the real planner's contract.
        knowledge = [] if corrective else list(self._knowledge)
        return Plan(objective=objective, stages=list(self._stages),
                    success_criteria=list(self._criteria),
                    strategy_rationale=self._rationale,
                    capability_needs=dict(self._needs),
                    direct_observation=dict(self._direct),
                    knowledge_needs=knowledge,
                    revision=dict(self._revision),
                    aspiration=dict(self._aspiration))
