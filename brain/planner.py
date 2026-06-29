"""Planner: objective -> task plan.

Uses a locked-down LLM session that must emit its plan through the structured
``emit_plan`` tool (so we get machine-readable output, not prose). For M1 the
plan is typically a single task; full dependency-graph planning is Phase 2.

A ``StubPlanner`` is provided for deterministic tests that should not call the
model.
"""
from __future__ import annotations

from typing import Iterable, Optional

from .models import Plan, Task

_SYSTEM = (
    "You are the planner of a self-organizing agent system. Decompose the user's objective "
    "into the SMALLEST sensible list of concrete tasks. For each task, set `capability_need` to "
    "the canonical key of the capability the task requires, choosing from the known keys when one "
    "fits. If a task needs live information from the internet, use the key \"web_search\". If a "
    "task needs no external capability, set `capability_need` to null. "
    "You MUST call the `emit_plan` tool exactly once with the full task list and must NOT answer "
    "in prose. Known capability keys: {known}."
)

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                    "capability_need": {"type": ["string", "null"]},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "description"],
            },
        }
    },
    "required": ["tasks"],
}


def _coerce_task(raw: dict) -> Task:
    return Task(
        id=str(raw.get("id") or "t1"),
        description=str(raw.get("description") or ""),
        capability_need=(raw.get("capability_need") or None),
        depends_on=list(raw.get("depends_on") or []),
    )


class Planner:
    def __init__(self, *, model: Optional[str] = None, known_capabilities: Optional[Iterable[str]] = None):
        self.model = model
        self.known = list(known_capabilities or ["web_search"])

    async def plan(self, objective: str) -> Plan:
        from claude_agent_sdk import query, tool, create_sdk_mcp_server
        from .factory import locked_session

        captured: dict = {}

        @tool("emit_plan", "Emit the decomposed task plan as structured data.", _PLAN_SCHEMA)
        async def emit_plan(args):
            captured["tasks"] = args.get("tasks", [])
            return {"content": [{"type": "text", "text": "plan recorded"}]}

        server = create_sdk_mcp_server(name="planner", version="1.0.0", tools=[emit_plan])
        options = locked_session(
            system_prompt=_SYSTEM.format(known=", ".join(self.known) or "(none yet)"),
            mcp_servers={"planner": server},
            allowed_tools=["mcp__planner__emit_plan"],
            model=self.model,
            max_turns=4,
        )

        async for _ in query(prompt=objective, options=options):
            pass

        tasks = [_coerce_task(t) for t in captured.get("tasks", []) if t]
        if not tasks:
            # Fallback: keep M1 robust if the model didn't emit a structured plan.
            tasks = [Task(id="t1", description=objective, capability_need="web_search")]
        return Plan(objective=objective, tasks=tasks)


class StubPlanner:
    """Deterministic planner for tests; returns the tasks it was given."""

    def __init__(self, tasks: list[Task]):
        self._tasks = tasks

    async def plan(self, objective: str) -> Plan:
        return Plan(objective=objective, tasks=list(self._tasks))
