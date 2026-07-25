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


class BrainSession:
    """Long-lived brain session. Call ``run_objective`` per objective; ``close``
    once at the end."""

    def __init__(
        self,
        *,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
        auto_export: bool = True,
    ):
        config.enforce_auth_invariant()
        self.output_fn = output_fn
        self.auto_export = auto_export

        self.tracer = Tracer(config.DB_PATH, config.RUNS_DIR,
                             engine_version=f"{config.ENGINE_VERSION}@{config.GIT_COMMIT}")
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

    def _janitor(self) -> None:
        """On wake, observe the environment and heal what a killed session left:
        leftover sandbox containers, runs stuck 'running', stale ledger rows."""
        removed = 0
        try:
            import docker

            client = (docker.DockerClient(base_url=config.DOCKER_HOST)
                      if config.DOCKER_HOST else docker.from_env())
            for c in client.containers.list(all=True, filters={"label": "brain.session"}):
                try:
                    c.remove(force=True)
                    removed += 1
                except Exception:  # noqa: BLE001
                    pass
            client.close()
        except Exception:  # noqa: BLE001  (docker unreachable — best effort)
            pass
        aborted = self.tracer.abort_stale_runs()
        reaped = self.ledger.mark_stale_reaped()
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

    def add_listener(self, fn) -> None:
        self.tracer.add_listener(fn)

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

        # Independent-trial mode: an operator running the SAME task repeatedly
        # as separate, comparable attempts (a benchmark, an A/B, a competition
        # re-run) does not want each run seeded from the last — that makes the
        # system re-derive the prior deliverable instead of solving afresh.
        # BRAIN_DISABLE_HISTORY_SEED skips relevance-based history seeding; the
        # capability REGISTRY (authored tools) still persists and is reused, so
        # only the prior-deliverable/workspace carry-over is suppressed.
        import os as _os

        if _os.environ.get("BRAIN_DISABLE_HISTORY_SEED"):
            hist_text, seed_run = "", None
            self.tracer.event("history.seed_disabled", "session", status="info",
                              reason="independent-trial mode (BRAIN_DISABLE_HISTORY_SEED)")
        else:
            hist_text, seed_run = history_mod.history_block(
                config.DB_PATH, config.RUNS_DIR, objective)
        # Session continuity: the previous run OF THIS SESSION outranks the
        # relevance-picked seed (a follow-up revises what was just made), and
        # its summary goes to the planner as SESSION CONTEXT so feedback is
        # recognized as feedback.
        session_context = None
        continues_session = bool(self._last_run)
        if self._last_run:
            seed_run = self._last_run["run_id"]
            session_context = (
                f"previous run id: {self._last_run['run_id']}\n"
                f"previous objective: {self._last_run['objective'][:600]}\n"
                f"previous result: {self._last_run['status']}\n"
                f"previous deliverable (payload head):\n---\n"
                f"{self._last_run['deliverable'][:2500]}\n---\n"
                f"files it produced (seeded under prior/{self._last_run['run_id']}/ in "
                f"this run's workspace): {self._last_run['files'][:12]}")
        # Open standing goals reach the planner so a continuing objective can
        # reference one (aspiration.goal_ref) instead of redeclaring it. They
        # ride the same planner block but must NOT imply continuity: an open
        # goal says what the operator is pursuing, never that this run follows
        # a previous turn. `continues_session` above is the only continuity
        # signal, and it is a fact, not an inference.
        from .memory.goals import goals_block
        gb = goals_block(self.goals)
        if gb:
            session_context = (session_context + "\n\n" + gb) if session_context else gb

        run = self.tracer.start_run(
            objective, model=(config.model_for("orchestrator") or "sdk-default"))
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
        blackboard = Blackboard(config.DB_PATH, session_id)
        manager = SandboxManager(session_id, self.ledger, tracer=self.tracer)
        # Catalog hygiene BEFORE planning (P1-B): the planner sees current,
        # qualified affordances — quarantined tools excluded, stale contracts
        # labeled, previously-failed needs carrying their failure — instead of
        # raw stored descriptions whose task residue anchors new plans.
        from .acquisition import validation_epoch
        from .registry import qualified_catalog
        known = qualified_catalog(self.registry, validation_epoch())
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
                                                continues_session=continues_session)
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
            out_dir = Path(config.RUNS_DIR) / session_id / "outputs"
            ws = Path(config.RUNS_DIR) / session_id / "workspace"
            try:
                deliverable = (out_dir / "DELIVERABLE.md").read_text()[:4000] \
                    if (out_dir / "DELIVERABLE.md").is_file() else (answer or "")[:2000]
            except OSError:
                deliverable = (answer or "")[:2000]
            produced = []
            if ws.is_dir():
                produced = [str(p.relative_to(ws)) for p in sorted(ws.rglob("*"))
                            if p.is_file()
                            and p.relative_to(ws).parts[0] not in ("prior", "input")][:20]
            self._last_run = {"run_id": session_id, "objective": objective,
                              "status": status, "deliverable": deliverable,
                              "files": produced}
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
        if not self.broker.request(
                action=f"pursue standing goal #{goal_id} with up to {budget} "
                       "SELF-INITIATED run(s)",
                reason="the objective requested autonomous iteration toward a "
                       "measurable goal; each iteration is a full run that spends "
                       "compute and may act on external services",
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
