"""BrainSession: one set of components, many objectives.

Both the one-shot CLI (`brain.main`) and the interactive REPL (`brain.repl`) run
objectives through this single path, so there is no duplicated wiring. Across a
session, connections are reused and each objective is a fresh traced run that can
draw on the history of earlier ones (continuity).
"""
from __future__ import annotations

import asyncio
import os
import urllib.request
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import config
from . import conversation as conversation_mod
from .approval import ApprovalBroker
from .ledger import Ledger
from .memory import history as history_mod
from .memory.blackboard import Blackboard
from .observability import Tracer
from .orchestrator import Orchestrator
from .planner import Planner
from .registry import Registry
from .resolver import Resolver
from .sandbox.manager import SandboxManager


@dataclass
class RunSummary:
    run_id: str
    status: str
    answer: str
    containers: int
    leaks: int
    used_history: bool
    seeded_from: Optional[str] = None
    capabilities: list = field(default_factory=list)


def _turn_referents(run_id: str, objective: str, status: str,
                    fallback_text: str = "") -> dict:
    """What a finished turn leaves behind for the next one to refer to.

    The deliverable head and the files it produced — read from the run's own
    directory, so this reconstructs identically whether the turn just finished
    in this process or is being read back out of a recorded conversation.
    """
    out_dir = Path(config.RUNS_DIR) / run_id / "outputs"
    ws = Path(config.RUNS_DIR) / run_id / "workspace"
    try:
        deliverable = (out_dir / "DELIVERABLE.md").read_text()[:4000] \
            if (out_dir / "DELIVERABLE.md").is_file() else (fallback_text or "")[:2000]
    except OSError:
        deliverable = (fallback_text or "")[:2000]
    produced = []
    if ws.is_dir():
        produced = [str(p.relative_to(ws)) for p in sorted(ws.rglob("*"))
                    if p.is_file()
                    and p.relative_to(ws).parts[0] not in ("prior", "input")][:20]
    return {"run_id": run_id, "objective": objective, "status": status,
            "deliverable": deliverable, "files": produced}


class BrainSession:
    """Long-lived brain session. Call ``run_objective`` per objective; ``close``
    once at the end."""

    def __init__(
        self,
        *,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
        auto_export: bool = True,
        resume: Optional[str] = None,
    ):
        config.enforce_auth_invariant()
        self.output_fn = output_fn
        self.auto_export = auto_export

        # This process IS one conversation. Minting the id here (rather than at
        # the first objective) means even a session that never runs anything
        # holds an identity, and every run it does host is grouped under it.
        # `resume` continues a RECORDED conversation instead of starting a new
        # one: its turns keep numbering, and what comes back is the record's
        # referents — never live state.
        self.conversation_id = resume or conversation_mod.new_id()
        self._live = conversation_mod.Liveness(config.LOCKS_DIR, self.conversation_id)
        if resume and not self._live.held:
            # Another live process is already appending to it. Two processes in
            # one conversation would interleave into a record neither of them
            # said, so this one starts its own instead of joining.
            output_fn(f"[conversation] {resume} is held by another live session — "
                      "starting a new conversation instead")
            resume = None
            self.conversation_id = conversation_mod.new_id()
            self._live = conversation_mod.Liveness(config.LOCKS_DIR, self.conversation_id)
        self.tracer = Tracer(config.DB_PATH, config.RUNS_DIR,
                             engine_version=f"{config.ENGINE_VERSION}@{config.GIT_COMMIT}",
                             conversation_id=self.conversation_id)
        self.ledger = Ledger(config.DB_PATH)
        self.registry = Registry(config.DB_PATH, config.CAPABILITIES_STORE)
        # Interactive gate: real prompts reach the human running the session.
        self.broker = ApprovalBroker(input_fn=input_fn, output_fn=output_fn, tracer=self.tracer)
        # Operator-provisioned credentials for UNATTENDED runs: an interactive
        # session collects a secret when a tool asks; a headless run cannot, so
        # the operator may pre-provision credentials via the environment —
        # BRAIN_SECRET_<NAME> (value) or BRAIN_SECRET_<NAME>_FILE (a path whose
        # CONTENTS are the value; keeps the secret off every command line). A
        # tool that declares it needs <NAME> then receives it exactly as if a
        # human had typed it: held in memory for the run, injected into the
        # sandbox at call time, and NEVER traced (only the name is recorded).
        self.broker.preseed_from_env()
        # Session revision memory: the previous objective and its deliverable,
        # so a follow-up like "I don't like the layout" is understood as
        # FEEDBACK to revise, not a fresh task to rebuild from scratch.
        self._last_run: Optional[dict] = None
        # Standing goals: aspirations that outlive a run. The ledger persists
        # them; the outcome loop (post-run) judges each run against its goal;
        # the pursuit loop (opt-in, user-granted budget) iterates on them.
        from .memory.goals import GoalLedger
        self.goals = GoalLedger(config.DB_PATH)
        self._pursuing = False   # guard: inner pursuit runs must not re-pursue
        self._janitor()
        if resume:
            self._restore_conversation(resume)

    def _janitor(self) -> None:
        """On wake, observe the environment and heal what a killed session left:
        leftover sandbox containers, runs stuck 'running', stale ledger rows.

        Scoped to conversations NOBODY is holding. This used to heal everything
        it could see, which was correct only while one process existed at a
        time: a second CLI force-removed the first's live sandboxes, marked its
        active run aborted, and rewrote its ledger rows — converting a leaking
        run into a clean-looking one.
        """
        live = conversation_mod.live_ids(config.LOCKS_DIR, own=self.conversation_id)
        removed = 0
        try:
            import docker

            client = (docker.DockerClient(base_url=config.DOCKER_HOST)
                      if config.DOCKER_HOST else docker.from_env())
            for c in client.containers.list(all=True, filters={"label": "brain.session"}):
                if (c.labels or {}).get("brain.conversation") in live:
                    continue        # another conversation is still using it
                try:
                    c.remove(force=True)
                    removed += 1
                except Exception:  # noqa: BLE001
                    pass
            client.close()
        except Exception:  # noqa: BLE001  (docker unreachable — best effort)
            pass
        aborted = self.tracer.abort_stale_runs(skip_conversations=live)
        reaped = self.ledger.mark_stale_reaped(keep_conversations=live)
        if aborted or reaped or removed:
            self.output_fn(
                f"[janitor]    healed leftovers from a previous session: "
                f"{aborted} run(s) marked aborted, {reaped} stale ledger row(s), "
                f"{removed} orphaned container(s)"
            )
            try:
                from .observability.view import refresh_index

                refresh_index(config.DB_PATH, config.RUNS_DIR)
            except Exception:  # noqa: BLE001
                pass

    def _restore_conversation(self, conversation_id: str) -> None:
        """Continue a recorded conversation: pick its last user turn back up.

        Only referents are restored — what was asked, what came back, which
        files it produced. Nothing live is resurrected: containers from that
        conversation are gone (its own process reaped them, or this process's
        janitor just did), and unfinished work is not resumed. Saying so is the
        point; a resume that implied otherwise would be the dishonest kind.
        """
        rows = conversation_mod.turns(config.DB_PATH, conversation_id)
        last = conversation_mod.last_user_turn(config.DB_PATH, conversation_id)
        if last is None:
            self.output_fn(f"[conversation] {conversation_id} has no recorded turns — "
                           "continuing it as a fresh one")
            return
        self._last_run = _turn_referents(
            last["run_id"], last["objective"] or "", last["status"] or "unknown",
            last["outcome"] or "")
        user_turns = sum(1 for r in rows if (r.get("origin") or "user") == "user")
        self.output_fn(
            f"[conversation] resumed {conversation_id} — {user_turns} turn(s) on record; "
            f"continuing from turn {last['turn'] or user_turns}: "
            f"{(last['objective'] or '')[:80]}")

    def switch_conversation(self, conversation_id: str) -> bool:
        """Continue a different recorded conversation from here on.

        Refuses when another live process already holds it: two processes
        appending turns to one conversation would interleave into a record
        neither of them said.
        """
        if conversation_id == self.conversation_id:
            return True
        live = conversation_mod.Liveness(config.LOCKS_DIR, conversation_id)
        if not live.held:
            live.release()
            self.output_fn(f"[conversation] {conversation_id} is held by another live "
                           "session — not switching")
            return False
        self._live.release()
        self._live = live
        self.conversation_id = conversation_id
        self.tracer.conversation_id = conversation_id
        self._last_run = None
        self._restore_conversation(conversation_id)
        return True

    def add_listener(self, fn) -> None:
        self.tracer.add_listener(fn)

    def _analyst(self):
        from .analyze import Analyst

        return Analyst(model=config.model_for("analyst"), tracer=self.tracer)

    def _conversation_turns(self) -> list[dict]:
        """This conversation's earlier turns, from the record.

        Read back rather than held in memory, so a resumed conversation and a
        live one are the same thing to everything downstream.
        """
        return [t for t in conversation_mod.turns(config.DB_PATH, self.conversation_id)
                if t.get("status") != "running"]

    def _conversation_context(self, referent: Optional[str]) -> Optional[str]:
        """What the planner is told about the conversation this turn sits in.

        Two separable facts, and they used to be fused into one: WHAT WAS SAID
        before (context — it may explain a reference, and never implies this
        turn continues it), and WHICH earlier turn this one's requirements
        actually refer to (a resolved referent — the only thing that licenses
        treating an earlier deliverable as material for this turn).
        """
        rows = [t for t in self._conversation_turns()
                if (t.get("origin") or "user") == "user"][-4:]
        blocks: list[str] = []
        if rows:
            lines = [f"- turn {t['turn']} (run {t['run_id']}, {t['status']}): "
                     f"{(t['objective'] or '')[:200]}" for t in rows]
            blocks.append("EARLIER TURNS OF THIS CONVERSATION (context only — this turn "
                          "does NOT necessarily continue them):\n" + "\n".join(lines))
        if referent:
            row = next((t for t in rows if t["run_id"] == referent), None)
            ref = _turn_referents(referent, (row or {}).get("objective", ""),
                                 (row or {}).get("status", "unknown"),
                                 (row or {}).get("outcome", "") or "")
            blocks.append(
                f"REFERENT — a stated requirement of THIS turn depends on what turn "
                f"{(row or {}).get('turn', '?')} produced (run {referent}):\n"
                f"it was asked: {ref['objective'][:600]}\n"
                f"it ended: {ref['status']}\n"
                f"what it produced (payload head):\n---\n{ref['deliverable'][:2500]}\n---\n"
                f"files it produced (seeded under prior/{referent}/ in this run's "
                f"workspace): {ref['files'][:12]}")
        return "\n\n".join(blocks) or None

    def _deliver_answer(self, run, objective: str, analysis) -> RunSummary:
        """Ship a turn that required nothing to be observed.

        It is recorded, costed and turn-numbered like any other run — an
        untraced reply would make the cheap path invisible to every view that
        exists. What it does NOT get is the certification vocabulary: no
        DELIVERABLE.md, no success criteria, no grounding review, and a status
        of its own rather than 'succeeded'. The engine's claim is that a
        deliverable's load-bearing claims trace to something a tool retained;
        this answer traces to nothing and says so in its own text.
        """
        from .analyze import answer_envelope

        answer = answer_envelope(analysis)
        out_dir = Path(config.RUNS_DIR) / run.run_id / "outputs"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "ANSWER.md").write_text(answer)
        except OSError:
            pass
        run.finish("answered", outcome=answer)
        self._last_run = _turn_referents(run.run_id, objective, "answered", answer)
        if self.auto_export:
            self._export(run.run_id)
        return RunSummary(
            run_id=run.run_id, status="answered", answer=answer, containers=0, leaks=0,
            used_history=False, seeded_from=None,
            capabilities=[c.name for c in self.registry.all()])

    async def run_objective(self, objective: str, files: Optional[list[str]] = None) -> RunSummary:
        # Secrets must enter via the hidden gate prompt — objectives are stored
        # forever (trace DB, run folders, history). Catch the mistake BEFORE
        # anything is recorded.
        from .observability.redact import contains_secret_shape

        if contains_secret_shape(objective):
            allowed = self.broker.request(
                action="run an objective that appears to CONTAIN A CREDENTIAL",
                reason="objectives are permanently logged (trace, run history); a secret pasted "
                       "here would be stored in masked form and should instead be provided at the "
                       "hidden secret prompt when the brain asks for it",
                requirements=["resubmit the objective without the secret (recommended: deny)"],
            )
            if not allowed:
                return RunSummary(
                    run_id="(not started)", status="refused",
                    answer=("Objective not run: it appears to contain a credential. Remove it and "
                            "resubmit — the brain will ask for the secret at a hidden prompt when "
                            "a tool needs it, keeping it out of all logs."),
                    containers=0, leaks=0, used_history=False, seeded_from=None,
                    capabilities=[c.name for c in self.registry.all()])

        # User-provided files: @mentions in the objective plus explicitly
        # attached paths. The engine delivers them into the run workspace and
        # states the facts; HOW to read/verify each format is the brain's job.
        from . import files as files_mod

        objective, mentioned = files_mod.parse_mentions(objective)
        # A file the user NAMES by path (backticked or bare — not only via @) is
        # a designated source too. Resolve each to a file the engine can reach:
        # a sandboxed tool only ever sees files the engine copies into the
        # workspace, so a path the engine cannot resolve here can NEVER reach a
        # tool, however the agent spells it (the technical reason a plainly-named
        # file was 'unreadable' — last run's inbox video, referenced by path,
        # never entered the workspace).
        objective, path_resolved, unresolved = files_mod.resolve_local_sources(objective)
        resolved = list(path_resolved)
        for tok in dict.fromkeys(mentioned + list(files or [])):
            hit = files_mod.resolve_file(tok)
            (resolved.append(str(hit)) if hit else unresolved.append(tok))
        # Unavoidable interaction: when a designated file cannot be accessed, ASK
        # for a correct path (bounded) instead of retrying blindly or starving
        # the run — the same interactive channel the credential gate uses.
        objective, resolved, missing = self._resolve_missing_files(
            objective, list(dict.fromkeys(unresolved)), resolved)
        attach_paths = list(dict.fromkeys(resolved))
        if missing:
            objective += (
                "\n\n[engine note] These file(s) you referenced could NOT be accessed and "
                "were NOT loaded into the run: " + ", ".join(missing) + ". Sandboxed tools "
                "can only read files the engine placed in the workspace. If the task depends "
                "on such a file, report a fatal blocker naming it — do NOT retry with guessed "
                "paths and NEVER fabricate its contents.")
            self.tracer.event("files.unavailable", "attach", status="failed", tokens=missing)

        # A self-initiated pursuit run is a run of the brain, not a turn of the
        # conversation: it is recorded, but it must not consume a turn ordinal.
        run = self.tracer.start_run(
            objective, model=(config.model_for("orchestrator") or "sdk-default"),
            origin="pursuit" if self._pursuing else "user")
        session_id = run.run_id
        if attach_paths:
            try:
                workspace = Path(config.RUNS_DIR) / session_id / "workspace"
                manifest = files_mod.materialize(attach_paths, workspace)
            except ValueError as e:
                run.finish("refused", outcome=str(e))
                return RunSummary(
                    run_id=session_id, status="refused", answer=f"Objective not run: {e}",
                    containers=0, leaks=0, used_history=False, seeded_from=None,
                    capabilities=[c.name for c in self.registry.all()])
            self.tracer.event("files.attached", session_id, status="info",
                              files=[m["path"] for m in manifest],
                              total_bytes=sum(m["bytes"] for m in manifest))
            objective += files_mod.manifest_note(manifest)

        # ANALYZE — the loop's first step, with its full output range. It either
        # answers (nothing must be observed) or escalates (something must be),
        # and it resolves which earlier turn, if any, this one's requirements
        # refer to. It runs BEFORE anything expensive is constructed: no Docker
        # handshake, no capability catalog, no planner.
        if self._pursuing:
            # A self-initiated iteration is not a turn: the user granted budget
            # for the brain to DO something toward a measured goal, and its
            # result is recorded as an attempt against that goal. Answering one
            # from memory would log an attempt that observed nothing.
            from .analyze import Analysis

            analysis = Analysis(reason="self-initiated iteration: the work is the point")
        else:
            analysis = await self._analyst().analyze(
                objective, turns=self._conversation_turns(), attached=attach_paths)
        if analysis.satisfied:
            return self._deliver_answer(run, objective, analysis)

        # Independent-trial mode: an operator running the SAME task repeatedly
        # as separate, comparable attempts (a benchmark, an A/B, a competition
        # re-run) does not want each run seeded from the last — that makes the
        # system re-derive the prior deliverable instead of solving afresh.
        # BRAIN_DISABLE_HISTORY_SEED suppresses BOTH carry-over paths (relevance
        # and referent); the capability REGISTRY still persists and is reused, so
        # only the prior-deliverable/workspace carry-over is suppressed.
        import os as _os

        independent = bool(_os.environ.get("BRAIN_DISABLE_HISTORY_SEED"))
        if independent:
            hist_text, seed_run = "", None
            self.tracer.event("history.seed_disabled", "session", status="info",
                              reason="independent-trial mode (BRAIN_DISABLE_HISTORY_SEED)")
        else:
            hist_text, seed_run = history_mod.history_block(
                config.DB_PATH, config.RUNS_DIR, objective)

        # Continuity is RESOLVED, not assumed. It used to be positional: the
        # previous run of this process became the seed and a designated source
        # unconditionally, so a turn that changed the subject still inherited a
        # stranger's deliverable as evidence about its own question. The
        # referent named by the analyze step outranks the relevance-picked seed;
        # a turn that refers to nothing inherits nothing.
        referent = None if independent else analysis.referent_run_id
        if referent:
            seed_run = referent
            self.tracer.event("referent.resolved", referent, status="info",
                              reason=analysis.reason)
        session_context = self._conversation_context(referent)
        # Open standing goals reach the planner so a continuing objective can
        # reference one (aspiration.goal_ref) instead of redeclaring it. They
        # ride the same planner block but must NOT imply continuity: an open
        # goal says what the operator is pursuing, never that this run follows
        # a previous turn.
        from .memory.goals import goals_block
        gb = goals_block(self.goals)
        if gb:
            session_context = (session_context + "\n\n" + gb) if session_context else gb

        blackboard = Blackboard(config.DB_PATH, session_id)
        manager = SandboxManager(session_id, self.ledger, tracer=self.tracer,
                                 conversation=self.conversation_id)
        # Catalog hygiene BEFORE planning (P1-B): the planner sees current,
        # qualified affordances — quarantined tools excluded, stale contracts
        # labeled, previously-failed needs carrying their failure — instead of
        # raw stored descriptions whose task residue anchors new plans.
        from .acquisition import validation_epoch
        from .registry import qualified_catalog
        known = qualified_catalog(self.registry, validation_epoch(),
                                  reuse_counts=self._reuse_counts())
        planner = Planner(model=config.model_for("planner"), known_capabilities=known,
                          tracer=self.tracer)
        from .supervisor import Supervisor

        orchestrator = Orchestrator(
            session=session_id, manager=manager, ledger=self.ledger, registry=self.registry,
            blackboard=blackboard, broker=self.broker, planner=planner,
            resolver=Resolver(self.registry), model=config.model_for("orchestrator"),
            use_builder=True, tracer=self.tracer,
            supervisor=Supervisor(model=config.model_for("supervisor"), tracer=self.tracer),
        )
        try:
            try:
                answer = await orchestrator.run(objective, history=hist_text,
                                                seed_run=seed_run,
                                                session_context=session_context,
                                                referent_resolved=bool(referent))
                # Objective-success class (#17) — operational completion is not
                # the same as meeting the objective.
                status = orchestrator.classification
            except asyncio.CancelledError:
                # User interrupt: record the truth, reap (via finally), re-raise.
                run.finish("aborted", outcome="(cancelled by user)")
                raise
            except Exception as e:  # noqa: BLE001
                answer = f"ERROR: {type(e).__name__}: {e}"
                status = "failed"
            leaks = self.ledger.find_leaks(session_id)
            if leaks:
                status = "failed"
            run.finish(status, outcome=answer)
            # Remember this run for the session: the next objective may be
            # feedback on THIS deliverable.
            self._last_run = _turn_referents(session_id, objective, status, answer)
            if self.auto_export:
                self._export(session_id)
            summary = RunSummary(
                run_id=session_id, status=status, answer=answer,
                containers=len(self.ledger.all(session_id)), leaks=len(leaks),
                used_history=bool(hist_text), seeded_from=seed_run,
                capabilities=[c.name for c in self.registry.all()],
            )
            # The outcome loop: judge this run against its standing goal (if
            # it declared one), record the attempt, surface the verdict and a
            # next-step proposal in the answer — and, ONLY under a user-granted
            # budget, pursue the goal with further self-initiated iterations.
            summary = await self._goal_outcome(
                summary, dict(getattr(orchestrator, "aspiration", {}) or {}))
            if not self._pursuing:
                summary = await self._maybe_pursue(summary)
            return summary
        finally:
            manager.close()
            blackboard.close()

    async def _goal_outcome(self, summary: RunSummary, aspiration: dict) -> RunSummary:
        """Piece 2+3 of the outcome loop: resolve the run's aspiration to a
        standing goal, judge the realized outcome against it, record the
        attempt, and append the goal banner (verdict + diagnosis + proposed
        next step) to the answer. Fail-open: any failure here leaves the
        summary exactly as delivered."""
        from . import goals as goals_mod

        self._goal_state = None
        if not aspiration:
            return summary
        try:
            goal = None
            if aspiration.get("goal_ref"):
                goal = self.goals.get(aspiration["goal_ref"])
            if goal is None and aspiration.get("metric"):
                goal = self.goals.get(self.goals.declare(
                    aspiration["metric"], aspiration.get("direction", "maximize"),
                    str(aspiration.get("target", "")), aspiration.get("measured_by", "")))
            if goal is None:
                return summary
            attempts = self.goals.attempts(goal["id"])
            best = self.goals.best_value(goal)
            outcome = await goals_mod.judge_outcome(
                goal, attempts, summary.answer, best=best,
                model=config.model_for("goal"))
            self.goals.record_attempt(goal["id"], summary.run_id,
                                      value=outcome["value"], verdict=outcome["verdict"],
                                      diagnosis=outcome["diagnosis"],
                                      next_step=outcome["next_step"])
            if outcome["verdict"] == "achieved":
                self.goals.mark(goal["id"], "achieved")
            self.tracer.event("goal.verdict", f"goal#{goal['id']}", status="info",
                              verdict=outcome["verdict"], value=outcome["value"],
                              best=best or "", run_id=summary.run_id)
            banner = goals_mod.goal_banner(
                goal, outcome, self.goals.best_value(goal),
                len(attempts) + 1, pursuing=self._pursuing or
                bool(aspiration.get("budget_attempts")))
            summary.answer = (summary.answer or "") + "\n" + banner
            # The banner is part of the run's record, not only the console.
            try:
                ans = Path(config.RUNS_DIR) / summary.run_id / "outputs" / "ANSWER.md"
                if ans.is_file():
                    ans.write_text(ans.read_text() + "\n" + banner)
            except OSError:
                pass
            self._goal_state = {"goal_id": goal["id"], "verdict": outcome["verdict"],
                                "next_step": outcome["next_step"],
                                "budget_attempts": aspiration.get("budget_attempts")}
        except Exception as e:  # noqa: BLE001 — the goal layer must never break delivery
            self.tracer.event("goal.error", "outcome", status="failed",
                              error=f"{type(e).__name__}: {str(e)[:200]}")
        return summary

    def _reuse_counts(self) -> dict:
        """How many distinct prior runs actually reused each capability.

        The library growing is not the claim; the library being REUSED is. The
        recorded decisions already say which tools earn their place, and the
        planner — the component that decides reuse-vs-build — was the one
        component never shown them. Best-effort: no history, no annotation."""
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
            try:
                return {row[0]: row[1] for row in conn.execute(
                    "SELECT json_extract(data_json,'$.capability'), COUNT(DISTINCT run_id) "
                    "FROM events WHERE kind='decision' "
                    "  AND json_extract(data_json,'$.choice')='reuse' "
                    "GROUP BY 1") if row[0]}
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — an annotation must never block planning
            return {}

    def _pursuit_cost_estimate(self, budget: int) -> str:
        """What `budget` more runs is likely to cost, from recorded history.

        The gate is the only place a human decides to spend unattended, and it
        asked for a COUNT. Recorded runs span roughly $0.01 to $17.30, so a
        count carries almost no information about the commitment; the median
        and the worst case do. Best-effort: no history, no estimate, and the
        approval prompt is unchanged."""
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
            try:
                costs = [r[0] for r in conn.execute(
                    "SELECT cost_usd FROM runs WHERE cost_usd > 0 "
                    "ORDER BY started_at DESC LIMIT 30") if r[0]]
            finally:
                conn.close()
            if not costs:
                return ""
            costs.sort()
            median = costs[len(costs) // 2]
            worst = costs[-1]
            return (f"typically ~${median * budget:.2f} total "
                    f"(median ${median:.2f}/run over the last {len(costs)} runs), "
                    f"up to ~${worst * budget:.2f} at the worst observed ${worst:.2f}/run")
        except Exception:  # noqa: BLE001 — an estimate must never block the gate
            return ""

    async def _maybe_pursue(self, summary: RunSummary) -> RunSummary:
        """Piece 4: bounded, user-authorized goal pursuit. Self-initiated runs
        happen ONLY when the objective granted a budget AND the human approves
        the autonomy at the gate — the budget is planner-extracted text and by
        itself must never authorize spending (it is prompt-injectable via a
        seeded prior deliverable). Every iteration is a full normal run
        (approval gates and prompts unchanged); the judge decides continue/stop
        and fails CLOSED; hard ceilings bound the loop regardless. Fail-open:
        any bookkeeping error stops pursuit and returns the delivered answer."""
        from . import goals as goals_mod

        state = getattr(self, "_goal_state", None)
        if not state or not state.get("budget_attempts"):
            return summary
        budget = min(max(1, int(state["budget_attempts"])),
                     config.GOAL_PURSUIT_MAX_ATTEMPTS)
        goal_id = state["goal_id"]
        # HARD GATE: autonomous, self-initiated runs are an outward, hard-to-
        # reverse capability — they cost real compute and may act on live
        # services. The extracted budget is a REQUEST; only the human at the
        # gate grants it. Deny-by-default (the broker's contract for auto runs).
        # A budget stated in RUNS is not a budget the approver can weigh: runs
        # in the recorded history span $0.01 to $17.30. Price the request from
        # this installation's own history so the human is consenting to an
        # amount, not to a count.
        estimate = self._pursuit_cost_estimate(budget)
        if not self.broker.request(
                action=f"pursue standing goal #{goal_id} with up to {budget} "
                       "SELF-INITIATED run(s)" + (f" — {estimate}" if estimate else ""),
                reason="the objective requested autonomous iteration toward a "
                       "measurable goal; each iteration is a full run that spends "
                       "compute and may act on external services"
                       + (f". Based on this installation's own history, {estimate}."
                          if estimate else ""),
                requirements=[f"approve up to {budget} autonomous run(s) "
                              "(deny to stop after this run and only PROPOSE next steps)"]):
            summary.answer += ("\n[goal pursuit] autonomous iteration was not "
                               "approved; stopping after this run. The proposed "
                               "next step above can be run manually.")
            self.tracer.event("goal.pursuit", f"goal#{goal_id}", status="info",
                              action="denied", reason="autonomy not approved at the gate")
            return summary
        self._pursuing = True
        done = 0
        try:
            while done < budget:
                goal = self.goals.get(goal_id)
                if goal is None or goal["status"] != "open":
                    break
                attempts = self.goals.attempts(goal_id)
                best = self.goals.best_value(goal)
                last_verdict = attempts[-1]["verdict"] if attempts else "unknown"
                d = await goals_mod.next_iteration_decision(
                    goal, attempts, attempts_left=budget - done, best=best,
                    model=config.model_for("goal"))
                self.tracer.event("goal.pursuit", f"goal#{goal_id}", status="info",
                                  action=d["action"], reason=d["reason"],
                                  iteration=done + 1, budget=budget)
                if d["action"] != "continue":
                    summary.answer += (f"\n[goal pursuit] stopped after {done} "
                                       f"self-initiated iteration(s): {d['reason']}")
                    break
                next_obj = goals_mod.compose_next_objective(
                    goal, last_verdict, best, d["next_approach"])
                self.output_fn(f"[goal]       pursuing goal #{goal_id} — iteration "
                               f"{done + 1}/{budget}: {d['reason'][:120]}")
                summary = await self.run_objective(next_obj)
                done += 1
                # An iteration that failed to judge itself against the goal
                # (planner declared no aspiration) must still appear in the
                # trajectory — the judge decides from attempts, and an
                # invisible attempt would make the loop repeat blindly.
                if len(self.goals.attempts(goal_id)) <= len(attempts):
                    self.goals.record_attempt(
                        goal_id, summary.run_id, verdict="unknown",
                        diagnosis="(run did not judge itself against the goal)")
        except asyncio.CancelledError:
            # Ctrl-C during an inner iteration: the OUTER run's answer is
            # already complete and recorded. Absorb the cancel (the inner run
            # marked itself aborted before re-raising), note it, and return the
            # finished deliverable instead of discarding it.
            summary.answer += (f"\n[goal pursuit] interrupted by user after {done} "
                               "self-initiated iteration(s); this answer was completed "
                               "before the interrupt.")
            task = asyncio.current_task()
            if task is not None and hasattr(task, "uncancel"):
                task.uncancel()
        except Exception as e:  # noqa: BLE001 — pursuit must never break delivery
            self.tracer.event("goal.error", "pursuit", status="failed",
                              error=f"{type(e).__name__}: {str(e)[:200]}")
            summary.answer += ("\n[goal pursuit] stopped early on an internal error; "
                               "the answer above is complete.")
        finally:
            self._pursuing = False
        return summary

    def _resolve_missing_files(self, objective: str, unresolved: list[str],
                               resolved: list[str]) -> tuple[str, list[str], list[str]]:
        """Ask the user for an accessible path for each designated file that
        could not be found — the file analogue of the credential gate. Bounded
        by FILE_ASK_ATTEMPTS; returns (objective, resolved, still_missing)."""
        from pathlib import Path as _Path

        from . import files as files_mod

        missing: list[str] = []
        for tok in unresolved:
            name = _Path(tok).name
            help_text = (
                f"The file '{tok}' named in your objective is not accessible to the brain. "
                f"Put it in the project's data/inbox/ folder and enter its path "
                f"(e.g. data/inbox/{name}), or enter any path inside the project the brain "
                "can read. Sandboxed tools can only use files the engine loads from there.")
            found: Optional[str] = None
            for _ in range(max(0, config.FILE_ASK_ATTEMPTS)):
                val = self.broker.collect_value(
                    name=f"an accessible path for {name}",
                    purpose=f"the file '{tok}' could not be accessed",
                    help_text=help_text)
                if not val:
                    break
                hit = files_mod.resolve_file(val)
                if hit is not None:
                    found = str(hit)
                    objective = objective.replace(tok, f"input/{_Path(hit).name}")
                    break
                self.broker.inform(f"Still cannot access '{val}'. The file must be inside "
                                   "the project (e.g. under data/inbox/).")
            if found:
                resolved.append(found)
            else:
                missing.append(tok)
        return objective, list(dict.fromkeys(resolved)), missing

    def _export(self, run_id: str) -> None:
        endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:6006/v1/traces")
        base = endpoint.split("/v1/")[0]
        try:
            urllib.request.urlopen(base, timeout=2)
        except Exception:
            return
        try:
            from .observability import otel_export

            n = otel_export.export_run(config.DB_PATH, run_id, endpoint)
            self.output_fn(f"[phoenix]    exported {n} spans — view at http://localhost:6006")
        except Exception as e:  # noqa: BLE001
            self.output_fn(f"[phoenix]    export skipped ({e})")

    def close(self) -> None:
        self.ledger.close()
        self.registry.close()
        self.goals.close()
        self.tracer.close()
        # Released last: while this is held, no other process may treat this
        # conversation's rows or containers as leftovers.
        self._live.release()
