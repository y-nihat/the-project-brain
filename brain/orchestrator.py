"""Executor / orchestrator: plan -> resolve -> acquire -> execute.

Flat model: the orchestrator coordinates the whole hierarchy in code. Agents are
driven as individual sessions and never spawn other agents. The orchestrator
owns shared state (the blackboard) and the capability registry.
"""
from __future__ import annotations

from typing import Optional

from . import factory
from .acquisition import acquire, make_builder_agent
from .capabilities.runtime import build_caps_server
from .models import CREATE, NOOP, REUSE, Capability


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

    async def run(self, objective: str) -> str:
        plan = await self.planner.plan(objective)
        self.blackboard.put("objective", objective)
        self.blackboard.put(
            "plan",
            [
                {"id": t.id, "description": t.description, "capability_need": t.capability_need}
                for t in plan.tasks
            ],
        )

        # Resolve each task; acquire any missing capability (build+verify+promote).
        caps: dict[str, Capability] = {}
        for task in plan.tasks:
            decision = self.resolver.resolve(task)
            if decision.kind == REUSE and decision.capability is not None:
                caps[decision.capability.name] = decision.capability
                self.blackboard.put(
                    f"decision:{task.id}", {"kind": "reuse", "capability": decision.capability.name}
                )
            elif decision.kind == CREATE and decision.capability_need:
                builder = make_builder_agent(self.model) if self.use_builder else None
                cap = await acquire(
                    decision.capability_need,
                    manager=self.manager,
                    registry=self.registry,
                    broker=self.broker,
                    builder=builder,
                )
                caps[cap.name] = cap
                self.blackboard.put(f"decision:{task.id}", {"kind": "create", "capability": cap.name})
            else:
                self.blackboard.put(f"decision:{task.id}", {"kind": NOOP})

        if not caps:
            answer = "No external capability was required for this objective."
            self.blackboard.put("answer", answer)
            return answer

        # Execute: a researcher agent answers the objective using the capabilities.
        caps_server = build_caps_server(list(caps.values()), manager=self.manager)
        allowed = [f"mcp__caps__{name}" for name in caps]
        answer = await self._run_researcher(objective, caps_server, allowed)
        self.blackboard.put("answer", answer)
        return answer

    async def _run_researcher(self, objective: str, caps_server, allowed: list[str]) -> str:
        from claude_agent_sdk import ResultMessage, query

        options = factory.locked_session(
            system_prompt=factory.RESEARCHER_PROMPT,
            mcp_servers={"caps": caps_server},
            allowed_tools=allowed,
            model=self.model,
            max_turns=8,
        )
        final = ""
        async for message in query(prompt=objective, options=options):
            if isinstance(message, ResultMessage):
                result = getattr(message, "result", None)
                if result:
                    final = result
        return final or "(no answer produced)"
