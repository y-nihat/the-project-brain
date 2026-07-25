"""CLI:  python -m brain.evals list | run [scenario ...] | score <run_id> <scenario>
         | score-corpus [scenario]"""
from __future__ import annotations

import asyncio
import sys

from . import runner, scenarios


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "list"

    if cmd == "list":
        print("scenarios:")
        for s in scenarios.SCENARIOS:
            print(f"  {s.id:<22} {s.intent}")
        print("\noffline scenarios (scored against recorded runs, no cost):")
        for s in scenarios.OFFLINE_SCENARIOS:
            print(f"  {s.id:<22} {s.intent}")
        print("\nrun live:      python -m brain.evals run [id ...]")
        print("score a run:   python -m brain.evals score <run_id> <scenario_id>")
        print("score history: python -m brain.evals score-corpus   (offline, $0)")
        return 0

    if cmd == "score":
        if len(argv) < 3:
            print("usage: python -m brain.evals score <run_id> <scenario_id>")
            return 2
        card = runner.score_run(argv[1], argv[2])
        print(runner.render_markdown([card]))
        return 0 if card.ok else 1

    if cmd == "score-corpus":
        sid = argv[1] if len(argv) > 1 else "engine-health"
        cards = runner.score_corpus(sid)
        if not cards:
            print("(no recorded runs to score)")
            return 0
        print(runner.render_corpus(cards))
        return 0

    if cmd == "run":
        from .. import config
        config.enforce_auth_invariant()
        ids = argv[1:] or None
        cards, out_dir = asyncio.run(runner.run_suite(ids, echo=True))
        print(runner.render_markdown(cards))
        print(f"scorecard written to {out_dir}")
        return 0 if all(c.ok for c in cards) else 1

    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
