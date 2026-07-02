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


def render_list(db_path: str | Path, limit: int = 20) -> str:
    runs = list_runs(db_path, limit)
    if not runs:
        return "(no runs recorded yet)"
    lines = [f"{'RUN':<18} {'STATUS':<10} {'CONT':>4} {'COST$':>8}  OBJECTIVE"]
    for r in runs:
        cost = r["cost_usd"] or 0.0
        lines.append(
            f"{r['run_id']:<18} {str(r['status']):<10} {r['n_containers'] or 0:>4} "
            f"{cost:>8.4f}  {(r['objective'] or '')[:60]}"
        )
    return "\n".join(lines)


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
        agents = data.get("agents") or []
        return f"{len(agents)} agent(s): " + "; ".join(
            f"{a.get('id')}({a.get('role')})"
            + (f"[{','.join(a.get('capabilities') or [])}]" if a.get("capabilities") else "")
            + (f"<-{','.join(a.get('depends_on') or [])}" if a.get("depends_on") else "")
            for a in agents if isinstance(a, dict)
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
        "| started (UTC) | run | status | cost $ | objective |",
        "|---|---|---|---|---|",
    ]
    for r in runs:
        started = (r["started_at"] or "")[:19].replace("T", " ")
        lines.append(
            f"| {started} | [{r['run_id']}]({r['run_id']}/) | {r['status']} "
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
            f"- **started:** {r['started_at']}\n\n"
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
    out.append(f"- **Started:** {run['started_at']}   **Duration:** {run['duration_ms']} ms")
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
