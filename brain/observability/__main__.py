"""CLI: python -m brain.observability list | show <run_id|--last> | follow <run_id>"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .. import config
from . import view


def _follow(run_id: str) -> int:
    path = Path(config.RUNS_DIR) / run_id / "trace.jsonl"
    if not path.exists():
        print(f"no trace stream at {path}")
        return 1
    print(f"following {path} (ctrl-c to stop)")
    with open(path) as f:
        f.seek(0, 2)  # tail: start at EOF
        try:
            while True:
                line = f.readline()
                if line:
                    print(line.rstrip())
                else:
                    time.sleep(0.4)
        except KeyboardInterrupt:
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m brain.observability")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list recent runs")
    p_list.add_argument("-n", "--limit", type=int, default=20)

    p_show = sub.add_parser("show", help="print a run's Markdown timeline")
    p_show.add_argument("run_id", nargs="?")
    p_show.add_argument("--last", action="store_true", help="show the most recent run")

    p_follow = sub.add_parser("follow", help="tail a run's live event stream")
    p_follow.add_argument("run_id")

    args = parser.parse_args(argv)
    db = config.DB_PATH

    if args.cmd == "list":
        print(view.render_list(db, args.limit))
        return 0

    if args.cmd == "show":
        run_id = args.run_id
        if args.last or not run_id:
            run_id = view.last_run_id(db)
            if not run_id:
                print("(no runs recorded yet)")
                return 1
        print(view.render_run(db, run_id))
        return 0

    if args.cmd == "follow":
        return _follow(args.run_id)

    return 2


if __name__ == "__main__":
    sys.exit(main())
