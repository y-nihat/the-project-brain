"""Executor / orchestrator: objective -> agent roster -> run the team.

Flat model: the orchestrator coordinates the whole hierarchy in code. The brain's
planner designs the agents; the orchestrator resolves/acquires each agent's
capabilities, synthesizes the agent (dynamic persona), runs it as its own session,
and passes upstream agents' outputs downstream via the blackboard. Agents never
spawn agents.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import config, factory
from .acquisition import acquire, make_builder_agent
from .acquisition import repair_capability as acquire_repair
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


def _persist_agent(runs_dir, session: str, spec: AgentSpec, system_prompt: str, output: str) -> None:
    """Agent outputs are first-class data-layer files, not just DB rows."""
    base = Path(runs_dir) / session
    outputs = base / "outputs"
    agents = base / "agents"
    outputs.mkdir(parents=True, exist_ok=True)
    agents.mkdir(parents=True, exist_ok=True)
    header = (
        f"# {spec.id} — {spec.role}\n\n"
        f"- **goal:** {spec.goal}\n"
        f"- **capabilities:** {spec.capabilities}\n"
        f"- **depends_on:** {spec.depends_on}\n\n---\n\n"
    )
    (outputs / f"{spec.id}.md").write_text(header + (output or ""))
    (agents / f"{spec.id}.prompt.md").write_text(system_prompt)


def _seed_prior_workspace(runs_dir, session: str, seed_run: str, *, max_bytes: int = 16 * 1024 * 1024) -> list[str]:
    """Copy a prior run's workspace files under `prior/<run_id>/` (bounded)."""
    src = Path(runs_dir) / seed_run / "workspace"
    dst = Path(runs_dir) / session / "workspace" / "prior" / seed_run
    copied: list[str] = []
    total = 0
    if not src.is_dir():
        return copied
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        size = p.stat().st_size
        if total + size > max_bytes:
            break
        rel = p.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(p.read_bytes())
        total += size
        copied.append(f"prior/{seed_run}/{rel}")
    return copied


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

    async def run(self, objective: str, history: Optional[str] = None,
                  seed_run: Optional[str] = None) -> str:
        with self.tracer.span("plan", "planner"):
            plan = await self.planner.plan(objective, history=history)
        self.blackboard.put("objective", objective)
        roster = [_spec_dict(a) for a in plan.agents]
        self.blackboard.put("plan", roster)
        self.tracer.event("plan.created", "plan", status="info", agents=roster)

        # Shared per-run workspace so file-producing tools can hand files to
        # file-consuming tools across agents.
        workspace = Path(config.RUNS_DIR) / self.session / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        # Cross-run continuity: make the most relevant prior run's files
        # available to this run's file tools.
        if seed_run:
            seeded = _seed_prior_workspace(config.RUNS_DIR, self.session, seed_run)
            if seeded:
                self.tracer.event("history.seeded", seed_run, status="info",
                                  files=seeded[:20], count=len(seeded))

        order = _topo_order(plan.agents)
        outputs: dict[str, str] = {}

        for spec in order:
            caps = await self._acquire_caps(spec)
            self.tracer.event(
                "agent.created", spec.id, status="info",
                role=spec.role, goal=spec.goal,
                capabilities=list(caps), depends_on=spec.depends_on,
            )
            upstream = {dep: outputs[dep] for dep in spec.depends_on if dep in outputs}
            out = await self._run_agent_with_reflection(spec, caps, objective, upstream, history, workspace)
            outputs[spec.id] = out
            self.blackboard.put(f"output:{spec.id}", out)

        answer = outputs.get(order[-1].id, "") if order else "No agents were produced."
        self.blackboard.put("answer", answer)
        answer_dir = Path(config.RUNS_DIR) / self.session / "outputs"
        answer_dir.mkdir(parents=True, exist_ok=True)
        (answer_dir / "ANSWER.md").write_text(answer or "")
        return answer or "(no answer produced)"

    async def _run_agent_with_reflection(self, spec, caps, objective, upstream, history, workspace) -> str:
        """Run an agent; if a tool it relied on degrades at runtime, reflect —
        repair that tool and re-run the agent — bounded, then best-effort."""
        from .capabilities.health import HealthMonitor
        from .observability import trace_query

        allowed = [f"mcp__caps__{name}" for name in caps]
        system_prompt = factory.agent_system_prompt(spec, allowed)
        repairs = 0
        out = ""
        while True:
            monitor = HealthMonitor()
            caps_server = (
                build_caps_server(list(caps.values()), manager=self.manager,
                                  workspace=workspace, monitor=monitor, tracer=self.tracer)
                if caps else None
            )
            options = factory.locked_session(
                system_prompt=system_prompt,
                mcp_servers={"caps": caps_server} if caps_server else {},
                allowed_tools=allowed, model=self.model, max_turns=10,
            )
            out = await trace_query(
                self.tracer, spec.id,
                factory.agent_user_prompt(objective, spec, upstream, history=history), options,
            )

            # Reflect: did any tool this agent used degrade in practice?
            sick = [(name, reason) for name in caps if (reason := monitor.unhealthy(name))]
            if not sick or repairs >= config.CAPABILITY_MAX_REPAIRS_PER_RUN:
                break
            repaired_any = False
            for name, reason in sick:
                self.tracer.event("capability.unhealthy", name, status="failed",
                                  reason=reason, stats=monitor.summary(name))
                try:
                    with self.tracer.span("capability.repair", name, reason=reason):
                        new_cap = await acquire_repair(
                            caps[name], reason, manager=self.manager, registry=self.registry,
                            broker=self.broker, model=self.model, tracer=self.tracer,
                        )
                    caps[name] = new_cap
                    repaired_any = True
                except Exception as e:  # noqa: BLE001
                    self.tracer.event("capability.repair", name, status="failed", error=str(e)[:300])
            if not repaired_any:
                break
            repairs += 1
            self.tracer.event("agent.rerun", spec.id, status="info", after_repair=repairs)

        _persist_agent(config.RUNS_DIR, self.session, spec, system_prompt, out)
        return out

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
                        description=f"{spec.role}: {spec.goal}",
                        model=self.model,
                        tracer=self.tracer,
                    )
                    caps[cap.name] = cap
        return caps
