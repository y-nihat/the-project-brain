"""One-shot CLI:  brain "<objective>"   (or: python -m brain.main "<objective>")

Runs one objective end-to-end through a BrainSession, streaming the run live,
and prints the answer plus a resource-accounting summary. For an interactive
session with history and follow-up turns, see `python -m brain.repl`.
"""
from __future__ import annotations

import asyncio
import sys

from .observability.stream import format_event
from .session import BrainSession

_USAGE = ('usage: brain "<objective>" [--quiet]   (or: python -m brain.main "<objective>")\n'
          '       python -m brain.repl                interactive session\n'
          '\n'
          '  --quiet   suppress the live event stream (the answer is still printed)')


async def _amain(objective: str, quiet: bool = False) -> int:
    session = BrainSession()
    try:
        if s := _history_note(session, objective):
            print(s)
        ticker_task = None
        if not quiet:
            # A run is silent for a median of ~6 minutes and a p90 of ~27. The
            # renderer is pure and already existed for the REPL; the documented
            # one-shot path simply never attached it, so a working system was
            # indistinguishable from a hung one. The ticker is the same story
            # one level down: the events say what JUST happened, and between
            # them nothing said what is happening now or what it has cost.
            from . import config
            from .observability.ticker import Ticker

            ticker = Ticker(output_fn=lambda s: print(s, flush=True),
                            stall_limit=config.AGENT_STALL_TIMEOUT)

            def _on_event(rec: dict) -> None:
                ticker.observe(rec)
                if line := format_event(rec):
                    print(line, flush=True)

            session.add_listener(_on_event)
            ticker_task = asyncio.create_task(ticker.loop())
        try:
            summary = await session.run_objective(objective)
        finally:
            if ticker_task is not None:
                ticker_task.cancel()
        print("\n==== ANSWER ====\n" + summary.answer)
        # The library's SIZE is the fact; naming all of it on every run buried
        # the run's own accounting under a list that has nothing to do with it.
        print(
            f"\n[accounting] run={summary.run_id} status={summary.status} "
            f"containers={summary.containers} leaks={summary.leaks} "
            f"library={len(summary.capabilities)} capabilit(ies)"
        )
        # What the run COST, stated plainly. The per-run README has carried
        # this since it was written; the answer never has.
        if cost := _cost_line(summary.run_id):
            print(cost)
        print(f"[trace]      python -m brain.observability show {summary.run_id}")
        # 'answered' is a completed turn that required nothing to be observed —
        # a success, and deliberately not the same word as one that was checked
        # against something a tool retained.
        return 0 if summary.status in ("succeeded", "answered") else 1
    finally:
        session.close()


def _cost_line(run_id: str) -> str:
    """Best-effort spend/duration readout for the finished run.

    One implementation, shared: the interactive path needs the same numbers,
    and for as long as this lived here it was the only path that printed any.
    """
    from . import config
    from .observability.cost import run_cost_line

    return run_cost_line(config.DB_PATH, run_id)


def _history_note(session: BrainSession, objective: str) -> str:
    import os

    from . import config
    from .memory import history as history_mod

    # Match the run's actual decision: in independent-trial mode the run does
    # not seed, so the note must not claim it did (it is display-only, computed
    # separately from BrainSession.run's own seed logic).
    if os.environ.get("BRAIN_DISABLE_HISTORY_SEED"):
        return "[memory]     independent-trial mode: not seeding from prior runs"
    hist, seed = history_mod.history_block(config.DB_PATH, config.RUNS_DIR, objective)
    if not hist:
        return ""
    return "[memory]     applying context from prior runs" + (f"; seeding workspace from {seed}" if seed else "")


def main() -> None:
    args = [a for a in sys.argv[1:] if a not in ("--quiet", "-q")]
    quiet = len(args) != len(sys.argv[1:])
    if not args:
        print(_USAGE)
        sys.exit(2)
    objective = " ".join(args)
    sys.exit(asyncio.run(_amain(objective, quiet=quiet)))


if __name__ == "__main__":
    main()
