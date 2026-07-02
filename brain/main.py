"""One-shot CLI:  python -m brain.main "<objective>"

Runs one objective end-to-end through a BrainSession and prints the answer plus a
resource-accounting summary. For an interactive, live-streamed session see
`python -m brain.repl`.
"""
from __future__ import annotations

import asyncio
import sys

from .session import BrainSession


async def _amain(objective: str) -> int:
    session = BrainSession()
    try:
        if s := _history_note(session, objective):
            print(s)
        summary = await session.run_objective(objective)
        print("\n==== ANSWER ====\n" + summary.answer)
        print(
            f"\n[accounting] run={summary.run_id} status={summary.status} "
            f"containers={summary.containers} leaks={summary.leaks} "
            f"capabilities={summary.capabilities}"
        )
        print(f"[trace]      python -m brain.observability show {summary.run_id}")
        return 0 if summary.status == "succeeded" else 1
    finally:
        session.close()


def _history_note(session: BrainSession, objective: str) -> str:
    from . import config
    from .memory import history as history_mod

    hist, seed = history_mod.history_block(config.DB_PATH, config.RUNS_DIR, objective)
    if not hist:
        return ""
    return "[memory]     applying context from prior runs" + (f"; seeding workspace from {seed}" if seed else "")


def main() -> None:
    if len(sys.argv) < 2:
        print('usage: python -m brain.main "<objective>"   (or: python -m brain.repl)')
        sys.exit(2)
    objective = " ".join(sys.argv[1:])
    sys.exit(asyncio.run(_amain(objective)))


if __name__ == "__main__":
    main()
