"""Executor / orchestrator: objective -> staged workflow -> run the stages.

The planner designs a DAG of stages (single | map | reduce | review); the
orchestrator executes them in dependency order:

  * ``single``  — one agent session (the degenerate workflow; simple tasks stay
    exactly as cheap as before);
  * ``map``     — fan-out decided at RUN time: the upstream stage's manifest is
    sharded and one worker session runs per shard, in bounded parallel
    ("constrain locally, aggregate globally");
  * ``reduce``  — consolidates the workers' notes, hierarchically when the
    volume exceeds one context;
  * ``review``  — evidence-grounded critic: checks the draft's claims against
    the underlying notes and demands fixes for a bounded number of rounds.

Agents never spawn agents; all width and depth decisions happen here, in code,
under hard caps. Every session keeps the existing safety rails (watchdog, call
budget, health monitor + runtime tool repair).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Awaitable, Callable, Optional

from . import config, factory, workflow
from .acquisition import acquire, make_builder_agent
from .acquisition import repair_capability as acquire_repair
from .capabilities.runtime import build_caps_server
from .models import CREATE, MAP, REDUCE, REUSE, REVIEW, AgentSpec, Capability, StageSpec
from .team import TEAM_TOOLS, TeamChannel, build_team_server

_VERDICT_RE = re.compile(r"VERDICT:\s*(approved|revise|needs-evidence)", re.IGNORECASE)


def _stage_dict(s: StageSpec) -> dict:
    return {"id": s.id, "pattern": s.pattern, "role": s.role, "goal": s.goal,
            "capabilities": s.capabilities, "depends_on": s.depends_on,
            "shard_size": s.shard_size, "shard_from": s.shard_from, "rounds": s.rounds}


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
    """Copy a prior run's workspace files under `prior/<run_id>/` (bounded).

    The prior run's own `prior/` subtree is skipped — otherwise seeds nest
    recursively, run after run.
    """
    src = Path(runs_dir) / seed_run / "workspace"
    dst = Path(runs_dir) / session / "workspace" / "prior" / seed_run
    copied: list[str] = []
    total = 0
    if not src.is_dir():
        return copied
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if rel.parts and rel.parts[0] == "prior":
            continue
        size = p.stat().st_size
        if total + size > max_bytes:
            break
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(p.read_bytes())
        total += size
        copied.append(f"prior/{seed_run}/{rel}")
    return copied


def _notes_block(notes: dict[str, str], limit: int) -> str:
    """Render worker notes for a prompt, hard-capped at `limit` chars."""
    parts: list[str] = []
    used = 0
    for wid, text in notes.items():
        chunk = f"\n[{wid}]:\n{text}\n"
        if used + len(chunk) > limit:
            parts.append(f"\n[engine note] remaining notes truncated at {limit} chars.")
            break
        parts.append(chunk)
        used += len(chunk)
    return "".join(parts)


def _batch_notes(notes: dict[str, str], limit: int) -> list[dict[str, str]]:
    """Greedily group notes into batches each fitting the char limit."""
    batches: list[dict[str, str]] = []
    current: dict[str, str] = {}
    used = 0
    for wid, text in notes.items():
        if current and used + len(text) > limit:
            batches.append(current)
            current, used = {}, 0
        current[wid] = text
        used += len(text)
    if current:
        batches.append(current)
    return batches


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
        session_runner=None,
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
        # Test seam: run an agent session; defaults to observability.trace_query.
        self._session_runner = session_runner
        self._sessions = 0

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #
    async def run(self, objective: str, history: Optional[str] = None,
                  seed_run: Optional[str] = None) -> str:
        with self.tracer.span("plan", "planner"):
            plan = await self.planner.plan(objective, history=history)
        self.blackboard.put("objective", objective)
        stages = workflow.topo_order(plan.stages)
        roster = [_stage_dict(s) for s in stages]
        self.blackboard.put("plan", roster)
        self.tracer.event("plan.created", "plan", status="info", stages=roster)

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

        self._sessions = 0
        self._stages_by_id = {s.id: s for s in stages}
        self._stage_notes: dict[str, dict[str, str]] = {}
        self._redo: dict[str, Callable[[str], Awaitable[str]]] = {}
        outputs: dict[str, str] = {}
        # Team-channel context: completed outputs + what a follow-up needs.
        self._outputs = outputs
        self._run_ctx = (objective, history, workspace)
        self._questions_answered = 0

        last_done: Optional[str] = None
        failure_note: Optional[str] = None
        for stage in stages:
            try:
                with self.tracer.span("stage", stage.id, pattern=stage.pattern) as sp:
                    out = await self._run_stage(stage, stages, outputs, objective, history, workspace)
                    # Surface the stage's result in the live view: mid-run output
                    # meant for the user (a how-to, a warning) must not be
                    # invisible until the run ends.
                    sp.set(output_head=(out or "")[:700])
            except Exception as e:  # noqa: BLE001
                # Best-effort delivery: a completed draft must never be vaporized
                # by a later stage's death. With nothing completed, fail loudly.
                self.tracer.event("stage.failed", stage.id, status="failed",
                                  error=f"{type(e).__name__}: {str(e)[:300]}")
                if last_done is None:
                    raise
                failure_note = (f"[engine note] stage '{stage.id}' failed "
                                f"({type(e).__name__}: {str(e)[:200]}); delivering the last "
                                f"completed stage's result ('{last_done}') as-is.")
                break
            outputs[stage.id] = out
            self.blackboard.put(f"output:{stage.id}", out)
            last_done = stage.id

        answer = outputs.get(last_done, "") if last_done else "No stages were produced."
        if failure_note:
            answer = (answer + "\n\n" + failure_note).strip()
        self.blackboard.put("answer", answer)
        answer_dir = Path(config.RUNS_DIR) / self.session / "outputs"
        answer_dir.mkdir(parents=True, exist_ok=True)
        (answer_dir / "ANSWER.md").write_text(answer or "")
        return answer or "(no answer produced)"

    # ------------------------------------------------------------------ #
    # stage dispatch
    # ------------------------------------------------------------------ #
    async def _run_stage(self, stage: StageSpec, stages: list[StageSpec],
                         outputs: dict[str, str], objective: str,
                         history: Optional[str], workspace: Path) -> str:
        if stage.pattern == MAP:
            return await self._run_map(stage, outputs, objective, history, workspace)
        if stage.pattern == REDUCE:
            return await self._run_reduce(stage, outputs, objective, history, workspace)
        if stage.pattern == REVIEW:
            return await self._run_review(stage, outputs, objective, history, workspace)
        return await self._run_single(stage, stages, outputs, objective, history, workspace)

    async def _run_single(self, stage: StageSpec, stages: list[StageSpec],
                          outputs: dict[str, str], objective: str,
                          history: Optional[str], workspace: Path) -> str:
        spec = AgentSpec(id=stage.id, role=stage.role, goal=stage.goal,
                         capabilities=stage.capabilities, depends_on=stage.depends_on)
        caps = await self._acquire_caps(spec)
        upstream = {d: outputs[d] for d in stage.depends_on if d in outputs}
        task_context = workflow.manifest_instruction() if workflow.feeds_a_map(stage.id, stages) else None
        out = await self._run_agent_with_reflection(
            spec, caps, objective, upstream, history, workspace, task_context=task_context)
        self._stage_notes[stage.id] = {stage.id: out}

        async def redo(critique: str, prior: str = "", _spec=spec, _caps=caps,
                       _up=upstream, _ctx=task_context) -> str:
            fix_ctx = ((_ctx or "")
                       + (f"\n\nYour previous draft:\n---\n{prior}\n---" if prior else "")
                       + "\n\nA reviewer requires these fixes — produce a corrected, "
                         "complete version of your result:\n" + critique)
            spec_fix = AgentSpec(id=f"{_spec.id}.fix", role=_spec.role, goal=_spec.goal,
                                 capabilities=_spec.capabilities, depends_on=_spec.depends_on)
            return await self._run_agent_with_reflection(
                spec_fix, _caps, objective, _up, history, workspace, task_context=fix_ctx)

        self._redo[stage.id] = redo
        return out

    async def _run_map(self, stage: StageSpec, outputs: dict[str, str], objective: str,
                       history: Optional[str], workspace: Path) -> str:
        source = stage.shard_from or (stage.depends_on[0] if stage.depends_on else "")
        items = workflow.parse_manifest(outputs.get(source, ""))
        if items is None:
            # Degrade honestly: no machine-readable manifest -> one worker over the raw output.
            self.tracer.event("map.no_manifest", stage.id, status="failed", source=source)
            items = [outputs.get(source, "")] if outputs.get(source) else []
        if not items:
            self.tracer.event("map.empty", stage.id, status="failed", source=source)
            return f"(map stage '{stage.id}': upstream '{source}' produced no work items)"

        shards = workflow.shard(items, stage.shard_size or config.MAP_DEFAULT_SHARD_SIZE,
                                config.MAP_MAX_WORKERS_PER_STAGE)
        self.tracer.event("map.fanout", stage.id, status="info",
                          items=len(items), workers=len(shards),
                          shard_size=max(len(s) for s in shards), source=source)

        # Acquire the worker template's capabilities ONCE, before fan-out, so
        # parallel workers never race to build the same tool.
        template = AgentSpec(id=stage.id, role=stage.role, goal=stage.goal,
                             capabilities=stage.capabilities, depends_on=[])
        caps = await self._acquire_caps(template)

        sem = asyncio.Semaphore(max(1, config.WORKER_PARALLELISM))
        total = len(shards)

        async def run_worker(k: int, shard_items: list, attempt: int = 1) -> str:
            wid = f"{stage.id}.w{k + 1:02d}" + (f".r{attempt}" if attempt > 1 else "")
            spec = AgentSpec(id=wid, role=stage.role, goal=stage.goal,
                             capabilities=stage.capabilities, depends_on=[])
            listing = "\n".join(f"- {workflow.render_item(it)}" for it in shard_items)
            task_context = (
                f"Your assigned work items (group {k + 1} of {total}; other workers handle "
                f"the rest — work ONLY on these, in depth):\n{listing}\n\n"
                "Return your findings as structured notes: per item, what you established, "
                "with evidence (quote or cite the source/location). Flag anything you could "
                "not verify. Do not pad; depth over breadth."
            )
            async with sem:
                return await self._run_agent_with_reflection(
                    spec, caps, objective, {}, history, workspace, task_context=task_context,
                    model=config.model_for("worker"))

        results = await asyncio.gather(
            *(run_worker(k, s) for k, s in enumerate(shards)), return_exceptions=True)

        notes: dict[str, str] = {}
        dead: list[int] = []
        for k, res in enumerate(results):
            wid = f"{stage.id}.w{k + 1:02d}"
            if isinstance(res, BaseException):
                dead.append(k)
                self.tracer.event("worker.failed", wid, status="failed", error=str(res)[:300])
                notes[wid] = f"(worker failed: {str(res)[:300]})"
            else:
                notes[wid] = res

        # A person re-assigns a sick colleague's pile: failed shards get one
        # fresh session before the stage gives up on their items.
        for attempt in range(2, config.MAP_RETRY_FAILED_SHARDS + 2):
            if not dead:
                break
            self.tracer.event("map.retry", stage.id, status="info",
                              attempt=attempt, shards=[k + 1 for k in dead])
            retries = await asyncio.gather(
                *(run_worker(k, shards[k], attempt=attempt) for k in dead),
                return_exceptions=True)
            still_dead: list[int] = []
            for k, res in zip(dead, retries):
                wid = f"{stage.id}.w{k + 1:02d}"
                if isinstance(res, BaseException):
                    still_dead.append(k)
                    self.tracer.event("worker.failed", f"{wid}.r{attempt}",
                                      status="failed", error=str(res)[:300])
                else:
                    notes[wid] = res
            dead = still_dead

        failed = len(dead)
        self._stage_notes[stage.id] = notes
        if failed == len(shards):
            return f"(map stage '{stage.id}': all {failed} workers failed)"

        digest = (f"[map:{stage.id}] {len(shards)} workers processed {len(items)} items "
                  f"({failed} failed). Full notes are in this run's outputs/, one file per worker.")
        return digest

    async def _run_reduce(self, stage: StageSpec, outputs: dict[str, str], objective: str,
                          history: Optional[str], workspace: Path) -> str:
        notes: dict[str, str] = {}
        for dep in stage.depends_on:
            notes.update(self._stage_notes.get(dep) or
                         ({dep: outputs[dep]} if dep in outputs else {}))
        if not notes:
            return f"(reduce stage '{stage.id}': nothing to consolidate)"

        limit = config.REDUCE_MAX_INPUT_CHARS
        batches = _batch_notes(notes, limit)
        if len(batches) > 1:
            # Hierarchical consolidation: independent partial merges run in
            # bounded parallel (same width as map workers), then the final one.
            self.tracer.event("reduce.batched", stage.id, status="info", batches=len(batches))
            sem = asyncio.Semaphore(max(1, config.WORKER_PARALLELISM))

            async def run_partial(j: int, batch: dict[str, str]) -> tuple[str, str]:
                bid = f"{stage.id}.b{j + 1:02d}"
                spec = AgentSpec(id=bid, role=stage.role,
                                 goal=("Partial consolidation towards: " + stage.goal +
                                       " Merge ONLY the notes given to you, faithfully; keep every "
                                       "source attribution; do not invent or drop findings."))
                ctx = "Notes to consolidate:\n" + _notes_block(batch, limit)
                async with sem:
                    out = await self._run_agent_with_reflection(
                        spec, {}, objective, {}, history, workspace, task_context=ctx)
                return bid, out

            done = await asyncio.gather(*(run_partial(j, b) for j, b in enumerate(batches)))
            notes = dict(done)

        spec = AgentSpec(id=stage.id, role=stage.role, goal=stage.goal,
                         capabilities=stage.capabilities, depends_on=stage.depends_on)
        caps = await self._acquire_caps(spec)
        ctx = ("Notes from the workers (merge faithfully; keep source attributions; "
               "flag disagreements between workers rather than papering over them):\n"
               + _notes_block(notes, limit))
        out = await self._run_agent_with_reflection(
            spec, caps, objective, {}, history, workspace, task_context=ctx)
        self._stage_notes[stage.id] = {stage.id: out}

        async def redo(critique: str, prior: str = "", _spec=spec, _caps=caps, _ctx=ctx) -> str:
            fix_ctx = (_ctx
                       + (f"\n\nYour previous draft:\n---\n{prior}\n---" if prior else "")
                       + "\n\nA reviewer requires these fixes — produce a corrected, "
                         "complete version of the deliverable:\n" + critique)
            spec_fix = AgentSpec(id=f"{_spec.id}.fix", role=_spec.role, goal=_spec.goal,
                                 capabilities=_spec.capabilities, depends_on=_spec.depends_on)
            return await self._run_agent_with_reflection(
                spec_fix, _caps, objective, {}, history, workspace, task_context=fix_ctx)

        self._redo[stage.id] = redo
        return out

    async def _run_review(self, stage: StageSpec, outputs: dict[str, str], objective: str,
                          history: Optional[str], workspace: Path) -> str:
        if not stage.depends_on:
            return f"(review stage '{stage.id}': nothing to review)"
        draft_id = stage.depends_on[0]
        draft = outputs.get(draft_id, "")

        # Evidence: the draft stage's underlying notes (its dependencies' outputs),
        # plus any additional dependencies the planner attached to the review.
        evidence: dict[str, str] = {}
        draft_stage = self._stages_by_id.get(draft_id)
        if draft_stage is not None:
            for dep in draft_stage.depends_on:
                evidence.update(self._stage_notes.get(dep) or {})
        for dep in stage.depends_on[1:]:
            evidence.update(self._stage_notes.get(dep) or
                            ({dep: outputs[dep]} if dep in outputs else {}))

        limit = config.REDUCE_MAX_INPUT_CHARS
        verdict = "revise"
        couriers_left = config.REVIEW_EVIDENCE_FETCHES
        try:
            for rnd in range(1, max(1, stage.rounds) + 1):
                spec = AgentSpec(id=f"{stage.id}.r{rnd}", role=stage.role, goal=stage.goal,
                                 capabilities=stage.capabilities, depends_on=stage.depends_on)
                caps = await self._acquire_caps(spec)
                ctx = (
                    "DRAFT under review:\n---\n" + draft + "\n---\n\n"
                    + ("EVIDENCE (the notes the draft must be supported by):\n"
                       + _notes_block(evidence, limit) + "\n\n" if evidence else "")
                    + "Check every load-bearing claim in the draft against the evidence. Unsupported "
                      "claims, numbers without a source, or claims resting on weak/promotional sources "
                      "must be flagged. APPROVE when the load-bearing claims are supported — style "
                      "preferences and minor wording are NOT grounds for revision. Your FIRST line must "
                      "be exactly one of: 'VERDICT: approved' — "
                      "'VERDICT: revise' (problems fixable from the existing evidence) — "
                      "'VERDICT: needs-evidence' (a load-bearing claim can only be settled by fetching "
                      "a specific missing source; then list EXACTLY what to fetch). If revising, follow "
                      "with a numbered list of specific, actionable fixes."
                )
                critique = await self._run_agent_with_reflection(
                    spec, caps, objective, {}, history, workspace, task_context=ctx)
                m = _VERDICT_RE.search(critique or "")
                verdict = m.group(1).lower() if m else "approved"
                self.tracer.event("review.verdict", stage.id, status="info",
                                  round=rnd, verdict=verdict, parsed=bool(m))
                if verdict == "approved":
                    return draft

                extra = ""
                if verdict == "needs-evidence" and couriers_left > 0:
                    couriers_left -= 1
                    courier = AgentSpec(
                        id=f"{stage.id}.evidence", role="Evidence courier",
                        goal="Obtain EXACTLY the missing evidence the reviewer listed; return it "
                             "with full source attribution. Fetch nothing else.",
                        capabilities=self._evidence_capabilities(draft_stage))
                    with self.tracer.span("review.evidence", stage.id):
                        fetched = await self._run_agent_with_reflection(
                            courier, await self._acquire_caps(courier), objective, {},
                            history, workspace,
                            task_context="The reviewer needs this evidence:\n" + critique)
                    evidence[courier.id] = fetched
                    extra = "\n\nNEWLY FETCHED EVIDENCE (courier):\n" + fetched[:20000]

                redo = self._redo.get(draft_id)
                if redo is None or rnd >= stage.rounds:
                    break
                draft = await redo(critique + extra, draft)
                outputs[draft_id] = draft
                self.tracer.event("review.revised", draft_id, status="info", round=rnd)
        except Exception as e:  # noqa: BLE001
            # The reviewer dying must not cost the user the draft: deliver it
            # unverified, honestly labeled.
            self.tracer.event("review.failed", stage.id, status="failed",
                              error=f"{type(e).__name__}: {str(e)[:300]}")
            return (draft + f"\n\n[engine note] the verification stage failed "
                            f"({type(e).__name__}: {str(e)[:200]}); this draft is delivered UNVERIFIED.")

        self.tracer.event("review.exhausted", stage.id, status="failed", rounds=stage.rounds)
        return (draft + f"\n\n[engine note] review flagged unresolved issues after "
                        f"{stage.rounds} round(s); see outputs/{stage.id}.r*.md for the critique.")

    def _evidence_capabilities(self, draft_stage: Optional[StageSpec]) -> list[str]:
        """Tools an evidence courier may use: whatever the draft's own evidence
        stages used (e.g. the map workers' fetch tool)."""
        caps: list[str] = []
        if draft_stage is not None:
            for dep in draft_stage.depends_on:
                st = self._stages_by_id.get(dep)
                if st is not None:
                    caps += [c for c in st.capabilities if c not in caps]
        return caps

    # ------------------------------------------------------------------ #
    # sessions
    # ------------------------------------------------------------------ #
    async def _answer_from_stage(self, stage_id: str, question: str) -> str:
        """Run a short, tool-less follow-up session of a completed stage's agent
        so a teammate's question gets answered by 'the person who did the work'."""
        objective, history, workspace = self._run_ctx
        st = self._stages_by_id.get(stage_id)
        prev = (self._outputs.get(stage_id) or "")[:40000]
        self._questions_answered += 1
        spec = AgentSpec(id=f"{stage_id}.q{self._questions_answered}",
                         role=(st.role if st else stage_id),
                         goal="Answer a teammate's question about your earlier work, "
                              "concisely and factually.")
        ctx = ("Your earlier output:\n---\n" + prev + "\n---\n\n"
               "Teammate's question: " + question +
               "\nAnswer strictly from your output and context; if you do not know, say so.")
        return await self._run_agent_with_reflection(
            spec, {}, objective, {}, history, workspace, task_context=ctx, team=False)

    async def _run_agent_with_reflection(self, spec, caps, objective, upstream, history,
                                         workspace, task_context: Optional[str] = None,
                                         model: Optional[str] = None, team: bool = True) -> str:
        """Run an agent; if a tool it relied on degrades at runtime, reflect —
        repair that tool and re-run the agent — bounded, then best-effort."""
        from .capabilities.health import HealthMonitor
        from .observability import trace_query

        if self._sessions >= config.RUN_MAX_AGENT_SESSIONS:
            self.tracer.event("run.session_cap", spec.id, status="failed",
                              cap=config.RUN_MAX_AGENT_SESSIONS)
            return (f"(not run: this run reached its cap of "
                    f"{config.RUN_MAX_AGENT_SESSIONS} agent sessions)")
        self._sessions += 1

        runner = self._session_runner or trace_query
        completed = list(getattr(self, "_outputs", {}) or {})
        channel = TeamChannel(agent_id=spec.id, completed_stages=completed,
                              ask_fn=self._answer_from_stage, blackboard=self.blackboard,
                              tracer=self.tracer) if team else None
        allowed = [f"mcp__caps__{name}" for name in caps]
        if channel is not None:
            allowed = allowed + TEAM_TOOLS
        session_secrets = getattr(self.broker, "secrets", {}) or {}
        ready = [c.name for c in caps.values()
                 if c.requires_credential and session_secrets.get(c.requires_credential)]
        system_prompt = factory.agent_system_prompt(
            spec, allowed, team_stages=(completed if channel is not None else None),
            ready_credentials=ready or None)
        repairs = 0
        out = ""
        while True:
            monitor = HealthMonitor()
            servers: dict = {}
            if caps:
                servers["caps"] = build_caps_server(
                    list(caps.values()), manager=self.manager,
                    workspace=workspace, monitor=monitor, tracer=self.tracer,
                    secrets=getattr(self.broker, "secrets", None))
            if channel is not None:
                servers["team"] = build_team_server(channel)
            options = factory.locked_session(
                system_prompt=system_prompt,
                mcp_servers=servers,
                allowed_tools=allowed,
                model=(model if model is not None else self.model),
                max_turns=config.AGENT_MAX_TURNS,
            )
            out = await runner(
                self.tracer, spec.id,
                factory.agent_user_prompt(objective, spec, upstream, history=history,
                                          task_context=task_context),
                options,
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
                    cap = decision.capability
                    # A reused credentialed tool still needs its secret THIS session.
                    if cap.requires_credential and cap.requires_credential not in (
                            getattr(self.broker, "secrets", {}) or {}):
                        granted = self.broker is not None and self.broker.request(
                            action=f"reuse capability '{cap.name}', which needs {cap.requires_credential}",
                            reason="the tool requires a credential; its value is held per session only",
                            requirements=[cap.requires_credential],
                        ) and self.broker.collect_secret(
                            cap.requires_credential, purpose=f"capability '{cap.name}'")
                        if not granted:
                            self.tracer.event("capability.unavailable", cap.name, status="denied",
                                              needs=cap.requires_credential, agent=spec.id)
                            continue  # the agent works without it and reports honestly
                    self.tracer.event("decision", need, status="info",
                                      choice="reuse", capability=cap.name, agent=spec.id)
                    caps[cap.name] = cap
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
