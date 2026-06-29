"""CLI entrypoint:  python -m brain.main "<objective>"

Wires the components together, runs one objective end-to-end, and prints the
answer plus a resource-accounting summary (so leaks are visible).
"""
from __future__ import annotations

import asyncio
import sys
import uuid

from . import config
from .approval import ApprovalBroker
from .ledger import Ledger
from .memory.blackboard import Blackboard
from .orchestrator import Orchestrator
from .planner import Planner
from .registry import Registry
from .resolver import Resolver
from .sandbox.manager import SandboxManager


async def _amain(objective: str) -> int:
    config.enforce_auth_invariant()

    session = f"run-{uuid.uuid4().hex[:8]}"
    ledger = Ledger(config.DB_PATH)
    registry = Registry(config.DB_PATH, config.CAPABILITIES_STORE)
    blackboard = Blackboard(config.DB_PATH, session)
    manager = SandboxManager(session, ledger)
    broker = ApprovalBroker()  # interactive, blocking
    known = [c.name for c in registry.all()] + ["web_search"]
    planner = Planner(model=config.BRAIN_MODEL, known_capabilities=sorted(set(known)))
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
    )

    try:
        answer = await orchestrator.run(objective)
        print("\n==== ANSWER ====\n" + answer)
        rows = ledger.all(session)
        leaks = ledger.find_leaks(session)
        print(
            f"\n[accounting] session={session} containers={len(rows)} "
            f"leaks={len(leaks)} capabilities={[c.name for c in registry.all()]}"
        )
        return 0 if not leaks else 1
    finally:
        manager.close()
        ledger.close()
        registry.close()
        blackboard.close()


def main() -> None:
    if len(sys.argv) < 2:
        print('usage: python -m brain.main "<objective>"')
        sys.exit(2)
    objective = " ".join(sys.argv[1:])
    sys.exit(asyncio.run(_amain(objective)))


if __name__ == "__main__":
    main()
