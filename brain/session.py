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
        attach_paths = list(dict.fromkeys(mentioned + list(files or [])))

        hist_text, seed_run = history_mod.history_block(config.DB_PATH, config.RUNS_DIR, objective)

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
                answer = await orchestrator.run(objective, history=hist_text, seed_run=seed_run)
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
            if self.auto_export:
                self._export(session_id)
            return RunSummary(
                run_id=session_id, status=status, answer=answer,
                containers=len(self.ledger.all(session_id)), leaks=len(leaks),
                used_history=bool(hist_text), seeded_from=seed_run,
                capabilities=[c.name for c in self.registry.all()],
            )
        finally:
            manager.close()
            blackboard.close()

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
        self.tracer.close()
