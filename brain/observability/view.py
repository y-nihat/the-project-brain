"""Render runs as human-readable Markdown timelines and list recent runs."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

_ICON = {
    "ok": "✓", "succeeded": "✓", "granted": "✓",
    "failed": "✗", "denied": "⛔", "blocked": "⛔",
    "running": "…", "info": "·",
}


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def list_runs(db_path: str | Path, limit: int = 20) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def last_run_id(db_path: str | Path) -> Optional[str]:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        return row["run_id"] if row else None
    finally:
        conn.close()


ARCHIVE_LABEL = "archive"


def _conv_label(row: dict) -> str:
    """How a run's conversation reads in a listing.

    Runs recorded before conversations existed are ARCHIVE, not "(none)":
    grouping them after the fact — by time adjacency or anything else — would
    fabricate conversations that never happened.
    """
    cid = row.get("conversation_id")
    if not cid:
        return ARCHIVE_LABEL
    turn = row.get("turn")
    origin = row.get("origin") or "user"
    mark = "" if origin == "user" else "+"
    return f"{cid}#{turn}{mark}" if turn else cid


def render_list(db_path: str | Path, limit: int = 20) -> str:
    runs = list_runs(db_path, limit)
    if not runs:
        return "(no runs recorded yet)"
    convs = [_conv_label(r) for r in runs]
    # Run ids are 45-65 chars; the old fixed width never aligned anything.
    w_run = max(len("RUN"), *(len(r["run_id"]) for r in runs))
    w_conv = max(len("CONVERSATION"), *(len(c) for c in convs))
    lines = [f"{'RUN':<{w_run}} {'CONVERSATION':<{w_conv}} {'STATUS':<11} "
             f"{'CONT':>4} {'COST$':>8}  OBJECTIVE"]
    for r, conv in zip(runs, convs):
        cost = r["cost_usd"] or 0.0
        lines.append(
            f"{r['run_id']:<{w_run}} {conv:<{w_conv}} {str(r['status']):<11} "
            f"{r['n_containers'] or 0:>4} {cost:>8.4f}  "
            f"{' '.join((r['objective'] or '').split())[:60]}"
        )
    return "\n".join(lines)


def list_conversations(db_path: str | Path, limit: int = 20) -> list[dict]:
    """Recorded conversations, most recently active first."""
    conn = _connect(db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT conversation_id, "
                "       COUNT(*) runs, "
                "       SUM(COALESCE(origin,'user')='user') turns, "
                "       MIN(started_at) started, MAX(started_at) last, "
                "       COALESCE(SUM(cost_usd),0) cost "
                "FROM runs WHERE conversation_id IS NOT NULL "
                "GROUP BY conversation_id ORDER BY last DESC LIMIT ?", (limit,)
            ).fetchall()
        except sqlite3.Error:
            return []       # a store written before conversations existed
        out = []
        for r in rows:
            first = conn.execute(
                "SELECT objective FROM runs WHERE conversation_id=? "
                "ORDER BY started_at LIMIT 1", (r["conversation_id"],)).fetchone()
            d = dict(r)
            d["opened_with"] = (first["objective"] if first else "") or ""
            out.append(d)
        return out
    finally:
        conn.close()


def render_conversations(db_path: str | Path, limit: int = 20) -> str:
    convs = list_conversations(db_path, limit)
    if not convs:
        return ("(no conversations recorded yet — runs from before conversations "
                "existed are archive)")
    w = max(len("CONVERSATION"), *(len(c["conversation_id"]) for c in convs))
    lines = [f"{'CONVERSATION':<{w}} {'TURNS':>5} {'RUNS':>5} {'COST$':>8}  "
             f"{'LAST ACTIVE':<19}  OPENED WITH"]
    for c in convs:
        lines.append(
            f"{c['conversation_id']:<{w}} {c['turns'] or 0:>5} {c['runs']:>5} "
            f"{c['cost'] or 0.0:>8.4f}  {local_ts(c['last']):<19}  "
            f"{c['opened_with'][:60]}")
    return "\n".join(lines)


def render_conversation(db_path: str | Path, conversation_id: str) -> str:
    """One conversation as its turns, in order."""
    conn = _connect(db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT * FROM runs WHERE conversation_id=? ORDER BY started_at",
                (conversation_id,)).fetchall()
        except sqlite3.Error:
            rows = []
    finally:
        conn.close()
    if not rows:
        return f"conversation {conversation_id} not found"
    total = sum(r["cost_usd"] or 0.0 for r in rows)
    turns = sum(1 for r in rows if (r["origin"] or "user") == "user")
    out = [f"# Conversation {conversation_id}", "",
           f"- **Turns:** {turns}   **Runs:** {len(rows)}   **Spend:** ${total:.4f}",
           f"- **Opened:** {local_ts(rows[0]['started_at'])}   "
           f"**Last active:** {local_ts(rows[-1]['started_at'])}", "",
           "## Turns", ""]
    for r in rows:
        origin = r["origin"] or "user"
        head = (f"### Turn {r['turn']}" if origin == "user"
                else f"### (self-initiated, under turn {r['turn']})")
        out += [f"{head} — {r['status']}   ${r['cost_usd'] or 0.0:.4f}   "
                f"{local_ts(r['started_at'])}",
                "", f"**Asked:** {(r['objective'] or '')[:600]}", "",
                f"**Answered:** {(r['outcome'] or '(no outcome recorded)')[:600]}", "",
                f"*(timeline: `python -m brain.observability show {r['run_id']}`)*", ""]
    return "\n".join(out)


def _summary(kind: str, data: dict) -> str:
    def g(k, d=""):
        v = data.get(k, d)
        return "" if v is None else v

    if kind == "decision":
        return f"{g('choice')} cap={g('capability')} {('— ' + str(g('reason'))) if g('reason') else ''}".strip()
    if kind == "sandbox.run":
        return (f"image={g('image')} net={g('network')} mem={g('mem')} cpus={g('cpus')} "
                f"exit={g('exit_code')} timed_out={g('timed_out')} ledger={str(g('ledger_id'))[:12]}")
    if kind == "image.build":
        return f"tag={g('tag')}"
    if kind == "approval.requested":
        return f"action={g('action')!r} reason={g('reason')!r}"
    if kind == "approval.resolved":
        return f"{g('outcome')}"
    if kind == "agent.session":
        return f"tokens_in={g('tokens_in')} tokens_out={g('tokens_out')} cost={g('cost_usd')} turns={g('turns')}"
    if kind == "agent.tool_call":
        return f"input_keys={g('input_keys')}"
    if kind == "capability.verify":
        return f"passed={g('passed')} {('— ' + str(g('detail'))) if g('detail') else ''}".strip()
    if kind == "capability.build":
        return f"source={g('source')} attempt={g('attempt')} → {g('artifact_path')}"
    if kind == "capability.promote":
        return f"source={g('source')} code={g('code_path')} snapshot={g('artifact_path')}"
    if kind == "plan.created":
        stages = data.get("stages") or data.get("agents") or []
        return f"{len(stages)} stage(s): " + "; ".join(
            f"{a.get('id')}[{a.get('pattern', 'single')}]({a.get('role')})"
            + (f"[{','.join(a.get('capabilities') or [])}]" if a.get("capabilities") else "")
            + (f"<-{','.join(a.get('depends_on') or [])}" if a.get("depends_on") else "")
            for a in stages if isinstance(a, dict)
        )
    if kind == "agent.created":
        caps = data.get("capabilities") or []
        deps = data.get("depends_on") or []
        return (f"role={g('role')} caps={list(caps)} "
                + (f"deps={list(deps)} " if deps else "")
                + f"— {g('goal')}").strip()
    if kind == "run":
        return f"model={g('model')}"
    # default: compact key=val for small payloads
    items = [f"{k}={v}" for k, v in data.items()]
    s = " ".join(items)
    return s[:160]


def local_ts(iso: str | None) -> str:
    """Render a stored UTC ISO timestamp in the machine's local timezone.

    Storage stays UTC (unambiguous, sortable); only display converts. The
    compose file mounts the host's /etc/localtime, so 'local' means the
    user's laptop, not the container."""
    if not iso:
        return ""
    try:
        import datetime as _dt

        return _dt.datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso[:19].replace("T", " ")


def refresh_index(db_path: str | Path, runs_dir: str | Path) -> None:
    """Keep data/runs/ navigable: INDEX.md (all runs, newest first), a LATEST
    pointer, and a per-run README.md. Best-effort — never breaks a run."""
    runs_dir = Path(runs_dir)
    runs = list_runs(db_path, limit=1000)
    if not runs:
        return

    lines = [
        "# Run index (newest first)",
        "",
        f"**Latest:** `{runs[0]['run_id']}` — {(runs[0]['objective'] or '')[:80]}",
        "",
        "| started | run | conversation | status | cost $ | objective |",
        "|---|---|---|---|---|---|",
    ]
    for r in runs:
        started = local_ts(r["started_at"])
        lines.append(
            f"| {started} | [{r['run_id']}]({r['run_id']}/) | {_conv_label(r)} | {r['status']} "
            f"| {r['cost_usd'] or 0.0:.2f} | {(r['objective'] or '').replace('|', '/')[:80]} |"
        )
    (runs_dir / "INDEX.md").write_text("\n".join(lines) + "\n")

    latest = runs_dir / "LATEST"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(runs[0]["run_id"])
    except OSError:
        (runs_dir / "LATEST.txt").write_text(runs[0]["run_id"] + "\n")

    r = runs[0]
    run_dir = runs_dir / r["run_id"]
    if run_dir.is_dir():
        (run_dir / "README.md").write_text(
            f"# {r['run_id']}\n\n"
            f"- **objective:** {r['objective']}\n"
            f"- **status:** {r['status']}   **cost:** ${r['cost_usd'] or 0.0:.2f}   "
            f"**containers:** {r['n_containers'] or 0}\n"
            f"- **started:** {local_ts(r['started_at'])}\n\n"
            "Contents: `trace.md` (timeline) · `outputs/` (agent outputs + ANSWER.md) · "
            "`workspace/` (files produced) · `artifacts/` (tool code authored)\n"
        )


def render_run(db_path: str | Path, run_id: str) -> str:
    conn = _connect(db_path)
    try:
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            return f"run {run_id} not found"
        events = conn.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
    finally:
        conn.close()

    out: list[str] = []
    out.append(f"# Run {run['run_id']}")
    out.append("")
    out.append(f"- **Objective:** {run['objective']}")
    out.append(f"- **Status:** {run['status']}   **Model:** {run['model'] or '(default)'}   "
               f"**Engine:** {run['engine_version']}")
    out.append(f"- **Started:** {local_ts(run['started_at'])}   **Duration:** {run['duration_ms']} ms")
    out.append(f"- **Containers:** {run['n_containers'] or 0}   **Approvals:** {run['n_approvals'] or 0}   "
               f"**Tokens:** {run['tokens_in'] or 0}/{run['tokens_out'] or 0}   "
               f"**Cost:** ${run['cost_usd'] or 0.0:.4f}")
    out.append("")
    out.append("## Timeline")
    out.append("")

    # Build the span/event tree by parent_id, ordered by seq.
    children: dict[Optional[str], list[sqlite3.Row]] = {}
    for e in events:
        children.setdefault(e["parent_id"], []).append(e)

    def walk(parent: Optional[str], depth: int) -> None:
        for e in children.get(parent, []):
            icon = _ICON.get(e["status"] or "info", "·")
            dur = f" ({e['duration_ms']} ms)" if e["duration_ms"] is not None else ""
            try:
                data = json.loads(e["data_json"] or "{}")
            except Exception:
                data = {}
            summary = _summary(e["kind"], data)
            line = f"{'  ' * depth}- {icon} `{e['kind']}/{e['name']}`{dur}"
            if summary:
                line += f" — {summary}"
            out.append(line)
            if e["error"]:
                out.append(f"{'  ' * (depth + 1)}- ✗ error: {e['error']}")
            walk(e["event_id"], depth + 1)

    walk(None, 0)
    out.append("")
    out.append("## Outcome")
    out.append("")
    out.append((run["outcome"] or "(none recorded)"))
    out.append("")
    out.append(f"*(full per-agent outputs: `runs/{run['run_id']}/outputs/`)*")
    return "\n".join(out)


def render_stats(db_path: str | Path) -> str:
    """Corpus-level answers to the questions the run rows already hold.

    Every number below was computable from day one and none was ever asked:
    the outcome mix, what each outcome COSTS, whether the engine is improving
    across versions, and which capabilities the library actually reuses. A
    growing reuse count is the product's central claim; a flat one is the
    signal to stop adding tools and fix reuse.
    """
    conn = _connect(db_path)
    try:
        out: list[str] = ["# Run statistics", ""]

        rows = list(conn.execute(
            "SELECT status, COUNT(*) n, COALESCE(SUM(cost_usd),0) spend, "
            "       AVG(cost_usd) avg_cost, AVG(duration_ms)/60000.0 avg_min "
            "FROM runs GROUP BY status ORDER BY spend DESC"))
        total_runs = sum(r["n"] for r in rows)
        total_spend = sum(r["spend"] for r in rows)
        unaccounted = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE status != 'running' AND cost_usd IS NULL"
        ).fetchone()[0]

        out += [f"**{total_runs} runs · ${total_spend:,.2f} recorded spend**", ""]
        if unaccounted:
            out += [f"> {unaccounted} run(s) have NO accounting — their spend is missing from "
                    f"every number here. Recover it with `python -m brain.observability "
                    f"backfill`.", ""]
        out += ["| outcome | runs | share | spend | share | avg cost | avg min |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for r in rows:
            out.append(
                f"| {r['status']} | {r['n']} | {100 * r['n'] / max(1, total_runs):.0f}% "
                f"| ${r['spend']:,.2f} | {100 * r['spend'] / max(1e-9, total_spend):.0f}% "
                f"| ${(r['avg_cost'] or 0):.2f} | {(r['avg_min'] or 0):.1f} |")
        out.append("")

        # Is it improving? Only comparable at constant task difficulty, so the
        # table reports the mix per version and says nothing stronger.
        # `answered` gets its own column. Folded into `other` beside
        # failed/aborted/refused, under a heading that asks whether the engine
        # is improving, the release that ADDED the cheap path would read as the
        # release that regressed.
        out += ["## By engine version", "",
                "| engine | runs | succeeded | answered | unverified | other | spend |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for r in conn.execute(
                "SELECT substr(engine_version,1,instr(engine_version||'@','@')-1) ev, "
                "       COUNT(*) n, "
                "       SUM(status='succeeded') ok, SUM(status='answered') ans, "
                "       SUM(status='unverified') unv, "
                "       COALESCE(SUM(cost_usd),0) spend "
                "FROM runs WHERE engine_version IS NOT NULL GROUP BY ev ORDER BY ev"):
            other = r["n"] - (r["ok"] or 0) - (r["ans"] or 0) - (r["unv"] or 0)
            out.append(f"| {r['ev']} | {r['n']} | {r['ok'] or 0} | {r['ans'] or 0} "
                       f"| {r['unv'] or 0} | {other} | ${r['spend']:,.2f} |")
        out += ["", "_Task difficulty is not constant across versions; read this as a mix, "
                    "not a trend._", ""]

        # The central claim, measured.
        out += ["## Capability reuse", ""]
        reuse = list(conn.execute(
            "SELECT json_extract(data_json,'$.capability') cap, COUNT(*) n, "
            "       COUNT(DISTINCT run_id) runs "
            "FROM events WHERE kind='decision' "
            "  AND json_extract(data_json,'$.choice')='reuse' "
            "GROUP BY cap ORDER BY n DESC"))
        authored = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='decision' "
            "  AND json_extract(data_json,'$.choice')='author'").fetchone()[0]
        reused_total = sum(r["n"] for r in reuse)
        if reused_total or authored:
            share = 100 * reused_total / max(1, reused_total + authored)
            out += [f"**{reused_total} reuse vs {authored} author decisions "
                    f"({share:.0f}% reuse)**", "",
                    "| capability | reuses | distinct runs |", "| --- | ---: | ---: |"]
            out += [f"| {r['cap']} | {r['n']} | {r['runs']} |" for r in reuse[:15]]
            try:
                # The registry shares this database in production but is a
                # separate component; a trace-only database has no such table.
                never = conn.execute(
                    "SELECT COUNT(*) FROM capabilities WHERE name NOT IN ("
                    "  SELECT json_extract(data_json,'$.capability') FROM events "
                    "  WHERE kind='decision' AND json_extract(data_json,'$.choice')='reuse')"
                ).fetchone()[0]
                out += ["", f"_{never} promoted capabilit(ies) have never been reused._", ""]
            except sqlite3.Error:
                out.append("")
        else:
            out += ["_(no resolve decisions recorded yet)_", ""]
        return "\n".join(out)
    finally:
        conn.close()
