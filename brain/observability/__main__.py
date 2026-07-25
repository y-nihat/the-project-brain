"""CLI: python -m brain.observability list | show <run_id|--last> | follow <run_id>
        | stats | backfill"""
from __future__ import annotations

import argparse
import os
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

    sub.add_parser("stats", help="corpus statistics: outcome mix, spend, capability reuse")

    sub.add_parser(
        "backfill",
        help="recompute accounting for finished runs whose totals were never written")

    p_follow = sub.add_parser("follow", help="tail a run's live event stream")
    p_follow.add_argument("run_id")

    p_export = sub.add_parser("export", help="export run(s) to an OTLP viewer (e.g. Phoenix)")
    p_export.add_argument("run_id", nargs="?")
    p_export.add_argument("--last", action="store_true", help="export the most recent run")
    p_export.add_argument("--all", action="store_true", help="export every recorded run")
    p_export.add_argument(
        "--endpoint",
        default=os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:6006/v1/traces"),
        help="OTLP HTTP traces endpoint (default: Phoenix on the compose network)",
    )

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

    if args.cmd == "stats":
        print(view.render_stats(db))
        return 0

    if args.cmd == "backfill":
        from .tracer import Tracer

        tracer = Tracer(db_path=db, runs_dir=config.RUNS_DIR)
        try:
            n = tracer.backfill_rollups()
        finally:
            tracer.close()
        print(f"recomputed accounting for {n} run(s) from their recorded events."
              if n else "every finished run already has accounting.")
        return 0

    if args.cmd == "follow":
        return _follow(args.run_id)

    if args.cmd == "export":
        from . import otel_export

        if args.all:
            run_ids = otel_export.list_run_ids(db)
        else:
            rid = args.run_id or (view.last_run_id(db) if args.last or not args.run_id else None)
            if not rid:
                print("(no runs recorded yet)")
                return 1
            run_ids = [rid]
        for rid in run_ids:
            n = otel_export.export_run(db, rid, args.endpoint)
            print(f"exported {rid}: {n} spans -> {args.endpoint}")
        print("view at: http://localhost:6006")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
