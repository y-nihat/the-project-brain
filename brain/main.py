"""CLI entrypoint:  python -m brain.main "<objective>"

Wires the components together, runs one objective end-to-end, and prints the
answer plus a resource-accounting summary (so leaks are visible).
"""
from __future__ import annotations

import asyncio
import sys

from . import config
from .approval import ApprovalBroker
from .ledger import Ledger
from .memory.blackboard import Blackboard
from .observability import Tracer
from .orchestrator import Orchestrator
from .planner import Planner
from .registry import Registry
from .resolver import Resolver
from .sandbox.manager import SandboxManager


async def _amain(objective: str) -> int:
    config.enforce_auth_invariant()

    tracer = Tracer(config.DB_PATH, config.RUNS_DIR, engine_version=config.ENGINE_VERSION)
    run = tracer.start_run(objective, model=config.BRAIN_MODEL)
    session = run.run_id  # ledger, blackboard, and the trace all share the run id

    ledger = Ledger(config.DB_PATH)
    registry = Registry(config.DB_PATH, config.CAPABILITIES_STORE)
    blackboard = Blackboard(config.DB_PATH, session)
    manager = SandboxManager(session, ledger, tracer=tracer)
    broker = ApprovalBroker(tracer=tracer)  # interactive, blocking
    known = {c.name: c.description for c in registry.all()}
    planner = Planner(model=config.BRAIN_MODEL, known_capabilities=known, tracer=tracer)
    resolver = Resolver(registry)
    orchestrator = Orchestrator(
        session=session,
        manager=manager,
        ledger=ledger,
        registry=registry,
        blackboard=blackboard,
        broker=broker,
        planner=planner,
        resolver=resolver,
        model=config.BRAIN_MODEL,
        use_builder=True,
        tracer=tracer,
    )

    try:
        try:
            answer = await orchestrator.run(objective)
            status = "succeeded"
        except Exception as e:  # noqa: BLE001
            answer = f"ERROR: {type(e).__name__}: {e}"
            status = "failed"

        leaks = ledger.find_leaks(session)
        if leaks:
            status = "failed"
        run.finish(status, outcome=answer)

        print("\n==== ANSWER ====\n" + answer)
        print(
            f"\n[accounting] run={session} status={status} "
            f"containers={len(ledger.all(session))} leaks={len(leaks)} "
            f"capabilities={[c.name for c in registry.all()]}"
        )
        print(f"[trace]      python -m brain.observability show {session}")
        return 0 if status == "succeeded" else 1
    finally:
        manager.close()
        ledger.close()
        registry.close()
        blackboard.close()
        tracer.close()


def main() -> None:
    if len(sys.argv) < 2:
        print('usage: python -m brain.main "<objective>"')
        sys.exit(2)
    objective = " ".join(sys.argv[1:])
    sys.exit(asyncio.run(_amain(objective)))


if __name__ == "__main__":
    main()
