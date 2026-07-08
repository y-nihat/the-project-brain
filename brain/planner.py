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
    "review: `rounds` (max fix cycles).\n"
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
    "itself. Writing, summarizing, analyzing, comparing, synthesizing, recommending, and "
    "reasoning are an agent's OWN abilities, NOT capabilities: such stages MUST have an "
    "empty `capabilities` list.\n"
    "When the user PROVIDES FILES (listed in the objective as workspace paths), design stages "
    "that actually OPEN and process them via file-handling capabilities — agents pass the "
    "listed paths through a tool's `files` argument; never assume a file's contents. A format "
    "the system has no tool for means naming a NEW capability for it. Treat verification as "
    "part of the work: whoever reads a file should check the extraction is complete and "
    "faithful (counts, structure, non-text content such as images) and surface anything that "
    "needs additional processing rather than silently ignoring it.\n"
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
    "Also compile `success_criteria`: a short list of CHECKABLE conditions the finished work "
    "must meet, derived from THIS objective's explicit demands — counts ('exactly 3 sections'), "
    "formats ('self-contained HTML'), fidelity ('every claim traceable to the provided file'), "
    "constraints ('under 40 words'), and self-consistency ('the final answer accurately "
    "describes the artifact produced'). Derive them from the request; never emit a fixed "
    "template. A reviewer will check the deliverable against them.\n"
    "You MUST call `emit_plan` exactly once with the full workflow and must NOT answer in prose."
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
                },
                "required": ["id", "pattern", "role", "goal"],
            },
        },
        "success_criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["stages"],
}


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

    async def plan(self, objective: str, history: Optional[str] = None) -> Plan:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        from .factory import locked_session
        from .observability import trace_query

        captured: dict = {}

        @tool("emit_plan", "Emit the staged workflow as structured data.", _PLAN_SCHEMA)
        async def emit_plan(args):
            captured["stages"] = args.get("stages", [])
            return {"content": [{"type": "text", "text": "plan recorded"}]}

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
            max_turns=4,
        )

        prompt = objective
        if history:
            prompt += (
                "\n\nPRIOR WORK by this system (you may design stages that build on it; its files "
                "can be seeded into the current workspace, and its capabilities are reusable):\n"
                + history
            )
        await trace_query(self.tracer, "planner", prompt, options)

        stages, warnings = normalize_stages(captured.get("stages", []))
        for w in warnings:
            self.tracer.event("plan.normalized", "planner", status="info", warning=w)
        if not stages:
            # Fallback: keep the system robust if the model didn't emit a workflow.
            stages = [StageSpec(id="researcher", pattern="single", role="researcher",
                                goal=objective, capabilities=["web_search"])]
        criteria = [str(c) for c in (captured.get("success_criteria") or []) if str(c).strip()]
        return Plan(objective=objective, stages=stages, success_criteria=criteria)


class StubPlanner:
    """Deterministic planner for tests; returns the workflow it was given."""

    def __init__(self, stages: list[StageSpec], success_criteria: Optional[list[str]] = None):
        self._stages = stages
        self._criteria = success_criteria or []

    async def plan(self, objective: str, history: Optional[str] = None) -> Plan:
        return Plan(objective=objective, stages=list(self._stages),
                    success_criteria=list(self._criteria))
