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

        self.tracer = Tracer(config.DB_PATH, config.RUNS_DIR, engine_version=config.ENGINE_VERSION)
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

    async def run_objective(self, objective: str) -> RunSummary:
        hist_text, seed_run = history_mod.history_block(config.DB_PATH, config.RUNS_DIR, objective)

        run = self.tracer.start_run(objective, model=config.BRAIN_MODEL)
        session_id = run.run_id
        blackboard = Blackboard(config.DB_PATH, session_id)
        manager = SandboxManager(session_id, self.ledger, tracer=self.tracer)
        known = {c.name: c.description for c in self.registry.all()}
        planner = Planner(model=config.BRAIN_MODEL, known_capabilities=known, tracer=self.tracer)
        orchestrator = Orchestrator(
            session=session_id, manager=manager, ledger=self.ledger, registry=self.registry,
            blackboard=blackboard, broker=self.broker, planner=planner,
            resolver=Resolver(self.registry), model=config.BRAIN_MODEL, use_builder=True,
            tracer=self.tracer,
        )
        try:
            try:
                answer = await orchestrator.run(objective, history=hist_text, seed_run=seed_run)
                status = "succeeded"
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
