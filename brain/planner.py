"""Planner: objective -> an agent roster.

A locked-down LLM session designs the *team* of agents needed for the objective,
emitting it through the structured ``emit_plan`` tool (machine-readable, not prose).
Each agent names its role, goal, the capability keys it needs, and which other
agents it depends on. The orchestrator then synthesizes and runs them.

``StubPlanner`` returns a fixed roster for deterministic tests.
"""
from __future__ import annotations

from typing import Iterable, Optional

from .models import AgentSpec, Plan

_SYSTEM = (
    "You are the planner of a self-organizing multi-agent system. Design the SMALLEST team of "
    "agents that accomplishes the user's objective. For each agent specify: `id` (short slug), "
    "`role`, `goal` (one sentence), `capabilities` (capability keys it needs), and `depends_on` "
    "(ids of agents whose output it requires).\n"
    "Capabilities are EXTERNAL tools an agent runs in a sandbox — e.g. searching the web, "
    "reading/parsing/generating files, or running code — things a plain LLM cannot do by itself. "
    "Writing, summarizing, analyzing, comparing, synthesizing, recommending, and reasoning are an "
    "agent's OWN abilities, NOT capabilities: such agents MUST have an empty `capabilities` list.\n"
    "Existing capabilities you may REUSE, but ONLY when the description genuinely matches the need:\n"
    "{known}\n"
    "For any other concrete external action, INVENT a new, SPECIFIC snake_case capability key that "
    "names exactly what it does (e.g. generate_pdf, extract_pdf_text, embed_and_search). Do NOT reuse "
    "an existing key whose description does not fit, and avoid vague umbrella names like "
    "'code_execution'. The system will build any new capability you name.\n"
    "You MUST call `emit_plan` exactly once with the full roster and must NOT answer in prose."
)

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "agents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "role": {"type": "string"},
                    "goal": {"type": "string"},
                    "capabilities": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "role", "goal"],
            },
        }
    },
    "required": ["agents"],
}


def _coerce_agent(raw: dict) -> AgentSpec:
    return AgentSpec(
        id=str(raw.get("id") or "agent"),
        role=str(raw.get("role") or "agent"),
        goal=str(raw.get("goal") or ""),
        capabilities=[str(c) for c in (raw.get("capabilities") or [])],
        depends_on=[str(d) for d in (raw.get("depends_on") or [])],
    )


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

        @tool("emit_plan", "Emit the team of agents as structured data.", _PLAN_SCHEMA)
        async def emit_plan(args):
            captured["agents"] = args.get("agents", [])
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
                "\n\nPRIOR WORK by this system (you may design agents that build on it; its files "
                "can be seeded into the current workspace, and its capabilities are reusable):\n"
                + history
            )
        await trace_query(self.tracer, "planner", prompt, options)

        agents = [_coerce_agent(a) for a in captured.get("agents", []) if a]
        if not agents:
            # Fallback: keep the system robust if the model didn't emit a roster.
            agents = [AgentSpec(id="researcher", role="researcher", goal=objective, capabilities=["web_search"])]
        return Plan(objective=objective, agents=agents)


class StubPlanner:
    """Deterministic planner for tests; returns the roster it was given."""

    def __init__(self, agents: list[AgentSpec]):
        self._agents = agents

    async def plan(self, objective: str, history: Optional[str] = None) -> Plan:
        return Plan(objective=objective, agents=list(self._agents))
