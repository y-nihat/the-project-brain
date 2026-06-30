"""Executor / orchestrator: objective -> agent roster -> run the team.

Flat model: the orchestrator coordinates the whole hierarchy in code. The brain's
planner designs the agents; the orchestrator resolves/acquires each agent's
capabilities, synthesizes the agent (dynamic persona), runs it as its own session,
and passes upstream agents' outputs downstream via the blackboard. Agents never
spawn agents.
"""
from __future__ import annotations

from typing import Optional

from . import factory
from .acquisition import acquire, make_builder_agent
from .capabilities.runtime import build_caps_server
from .models import CREATE, REUSE, AgentSpec, Capability


def _topo_order(agents: list[AgentSpec]) -> list[AgentSpec]:
    """Dependency order (Kahn-ish DFS); tolerant of missing deps and cycles."""
    by_id = {a.id: a for a in agents}
    order: list[AgentSpec] = []
    visited: set[str] = set()
    on_stack: set[str] = set()

    def visit(a: AgentSpec) -> None:
        if a.id in visited or a.id in on_stack:
            return
        on_stack.add(a.id)
        for dep in a.depends_on:
            if dep in by_id:
                visit(by_id[dep])
        on_stack.discard(a.id)
        visited.add(a.id)
        order.append(a)

    for a in agents:
        visit(a)
    return order


def _spec_dict(a: AgentSpec) -> dict:
    return {"id": a.id, "role": a.role, "goal": a.goal,
            "capabilities": a.capabilities, "depends_on": a.depends_on}


class Orchestrator:
    def __init__(
        self,
        *,
        session: str,
        manager,
        ledger,
        registry,
        blackboard,
        broker,
        planner,
        resolver,
        model: Optional[str] = None,
        use_builder: bool = True,
        tracer=None,
    ):
        self.session = session
        self.manager = manager
        self.ledger = ledger
        self.registry = registry
        self.blackboard = blackboard
        self.broker = broker
        self.planner = planner
        self.resolver = resolver
        self.model = model
        self.use_builder = use_builder
        from .observability import NullTracer

        self.tracer = tracer or NullTracer()

    async def run(self, objective: str) -> str:
        with self.tracer.span("plan", "planner"):
            plan = await self.planner.plan(objective)
        self.blackboard.put("objective", objective)
        roster = [_spec_dict(a) for a in plan.agents]
        self.blackboard.put("plan", roster)
        self.tracer.event("plan.created", "plan", status="info", agents=roster)

        order = _topo_order(plan.agents)
        outputs: dict[str, str] = {}

        for spec in order:
            caps = await self._acquire_caps(spec)
            caps_server = build_caps_server(list(caps.values()), manager=self.manager) if caps else None
            allowed = [f"mcp__caps__{name}" for name in caps]

            self.tracer.event(
                "agent.created", spec.id, status="info",
                role=spec.role, goal=spec.goal,
                capabilities=list(caps), depends_on=spec.depends_on,
            )

            upstream = {dep: outputs[dep] for dep in spec.depends_on if dep in outputs}
            options = factory.locked_session(
                system_prompt=factory.agent_system_prompt(spec, allowed),
                mcp_servers={"caps": caps_server} if caps_server else {},
                allowed_tools=allowed,
                model=self.model,
                max_turns=10,
            )

            from .observability import trace_query

            out = await trace_query(
                self.tracer, spec.id, factory.agent_user_prompt(objective, spec, upstream), options
            )
            outputs[spec.id] = out
            self.blackboard.put(f"output:{spec.id}", out)

        answer = outputs.get(order[-1].id, "") if order else "No agents were produced."
        self.blackboard.put("answer", answer)
        return answer or "(no answer produced)"

    async def _acquire_caps(self, spec: AgentSpec) -> dict[str, Capability]:
        caps: dict[str, Capability] = {}
        for need in spec.capabilities:
            with self.tracer.span("resolve", need, agent=spec.id):
                decision = self.resolver.resolve(need)
                if decision.kind == REUSE and decision.capability is not None:
                    self.tracer.event("decision", need, status="info",
                                      choice="reuse", capability=decision.capability.name, agent=spec.id)
                    caps[decision.capability.name] = decision.capability
                elif decision.kind == CREATE and decision.capability_need:
                    builder = make_builder_agent(self.model, self.tracer) if self.use_builder else None
                    cap = await acquire(
                        need,
                        manager=self.manager,
                        registry=self.registry,
                        broker=self.broker,
                        builder=builder,
                        tracer=self.tracer,
                    )
                    caps[cap.name] = cap
        return caps
