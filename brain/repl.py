"""Interactive terminal for the brain:  python -m brain.repl

Type an objective and watch the brain work live — plan, agents, tool authoring,
verification, answer — all streamed as it happens and recorded under data/.
Commands: /help /runs /show /caps /last /quit
"""
from __future__ import annotations

import asyncio
import signal
import time
from typing import Optional

# Compact glyphs for the live stream.
_ICON = {"ok": "✓", "succeeded": "✓", "granted": "✓", "passed": "✓",
         "failed": "✗", "denied": "⛔", "running": "…", "info": "·"}


def format_event(record: dict) -> Optional[str]:
    """Render one trace record as a compact live-progress line, or None to skip.

    Pure and unit-tested — the REPL just prints whatever this returns.
    """
    kind = record.get("kind")
    name = record.get("name") or ""
    status = record.get("status") or "info"
    data = record.get("data") or {}
    phase = record.get("phase")
    dur = record.get("duration_ms")
    dur_s = f" ({dur/1000:.1f}s)" if isinstance(dur, (int, float)) and dur else ""

    # Spans emit a start and an end; render only the informative side per kind.
    if kind == "run":
        return None  # the REPL frames the run itself
    if kind == "plan.created":
        stages = data.get("stages") or data.get("agents") or []
        shape = ", ".join(f"{s.get('id', '?')}({s.get('pattern', 'single')})" for s in stages)
        return f"  📋 plan: {len(stages)} stage(s) — {shape}"
    if kind == "stage":
        if phase == "start":
            return f"  ▶ stage {name} [{data.get('pattern', 'single')}]"
        line = f"  ■ stage {name} done{dur_s}"
        head = (data.get("output_head") or "").strip()
        if head:
            body = "\n".join(f"  │ {ln}" for ln in head.splitlines()[:14])
            line += f"\n{body}\n  └─ (full output: outputs/{name}.md)"
        return line
    if kind == "run.classification":
        cls = data.get("classification")
        icon = "✅" if cls == "succeeded" else "⚠"
        return f"  {icon} result: {str(cls).upper()} — {str(data.get('reason', ''))[:90]}"
    if kind == "plan.criteria":
        crit = data.get("criteria") or []
        return f"  ✓ success criteria ({len(crit)}): " + " · ".join(str(c)[:50] for c in crit[:4])
    if kind == "map.fanout":
        return (f"     ⑃ fan-out: {data.get('items')} items → {data.get('workers')} workers "
                f"(≤{data.get('shard_size')} items each)")
    if kind in ("map.no_manifest", "map.empty"):
        return f"     ⚠ {name}: {'no machine-readable manifest upstream' if kind == 'map.no_manifest' else 'no work items'}"
    if kind == "worker.failed":
        return f"     ✗ worker {name} failed: {str(data.get('error', ''))[:80]}"
    if kind == "reduce.batched":
        return f"     ⑂ consolidating hierarchically in {data.get('batches')} batches"
    if kind == "review.verdict":
        v = data.get("verdict")
        icon = "✓" if v == "approved" else "✎"
        return f"     {icon} review round {data.get('round')}: {v}"
    if kind == "review.revised":
        return f"     ↻ draft revised after review ({name})"
    if kind == "review.evidence" and phase == "start":
        return "     ⇣ reviewer demanded missing evidence — courier fetching…"
    if kind == "review.failed":
        return f"     ⚠ reviewer died ({str(data.get('error', ''))[:60]}) — draft delivered UNVERIFIED"
    if kind == "team.question":
        return f"     🗨 {name} asks {data.get('to')}: {str(data.get('question', ''))[:70]}"
    if kind == "team.blocker":
        return f"     ✋ {name} reports a blocker: {str(data.get('issue', ''))[:70]}"
    if kind == "agent.salvaged":
        return f"     ♻ {name} ran out of budget — partial findings salvaged ({data.get('chars')} chars)"
    if kind == "map.retry":
        return f"     ↻ retrying failed shard(s) {data.get('shards')} with fresh sessions"
    if kind == "stage.failed":
        return f"  ⚠ stage {name} failed ({str(data.get('error', ''))[:70]}) — delivering best effort"
    if kind == "review.exhausted":
        return f"     ⚠ review rounds exhausted — answer carries an honesty note"
    if kind == "run.session_cap":
        return f"  ⚠ session cap reached — {name} not run"
    if kind == "agent.created":
        caps = data.get("capabilities") or []
        cap_s = f" [{', '.join(caps)}]" if caps else " [no tools — reasons unaided]"
        return f"  🤖 agent ▸ {data.get('role', name)}{cap_s}"
    if kind == "tool.result":
        r = data.get("result")
        n = data.get("count")
        if r == "ok":
            return f"         → ok ({n} result{'s' if n != 1 else ''})"
        return f"         → {r}{' — tool may be degraded' if r == 'empty' else ''}"
    if kind == "capability.unhealthy":
        return f"     ⚠ {name} looks unhealthy ({str(data.get('reason', ''))[:80]}) — repairing…"
    if kind == "agent.rerun":
        return f"     ↻ re-running {name} with the repaired tool"
    if kind == "decision":
        if data.get("choice") == "reuse":
            return f"     ↩ reuse tool: {data.get('capability')}"
        if data.get("choice") == "author":
            return f"     ✎ needs a new tool: {data.get('capability')} — authoring…"
        return None
    if kind == "agent.session" and phase == "start" and name.startswith("developer:"):
        return f"       developer writing {name.split(':',1)[1]}…"
    if kind == "capability.build" and phase == "start":
        return f"       build ▸ {name} (attempt {data.get('attempt', '?')}, {data.get('source','?')})"
    if kind == "capability.verify" and phase != "start":
        ok = data.get("passed")
        tail = f" — {data.get('detail','')}" if not ok else ""
        return f"       verify {_ICON['passed'] if ok else _ICON['failed']} {name}{dur_s}{tail[:100]}"
    if kind == "capability.promote" and phase != "start":
        return f"       promoted {name} ✓ (now reusable)"
    if kind == "sandbox.run" and phase != "start":
        oom = " OOM!" if data.get("oom") else ""
        return f"       ▸ ran {name} exit={data.get('exit_code')}{oom}{dur_s}"
    if kind == "approval.requested":
        return f"  ⚠ approval needed: {data.get('action')}"
    if kind == "approval.resolved":
        return f"  ⚠ approval {status}"
    if kind == "supervisor.consult":
        if data.get("action") == "proceed":
            return None  # silent when nothing needs doing
        return f"  🧭 supervisor [{name}]: {data.get('action')} — {str(data.get('reason', ''))[:80]}"
    if kind == "supervisor.redo":
        return f"  🧭 supervisor sent {name} back: {str(data.get('guidance', ''))[:80]}"
    if kind == "supervisor.error":
        return None  # fail-open is silent; the trace has it
    if kind == "user.informed":
        return None  # the broker already printed the message itself
    if kind == "files.attached":
        return f"  📎 files in workspace: {', '.join(data.get('files') or [])}"
    if kind == "secret.requested":
        return f"  🔑 secret needed: {name} — {str(data.get('purpose', ''))[:70]}"
    if kind == "secret.provided":
        return (f"  🔑 {name} provided (held in memory for this session only)"
                if status == "granted" else f"  🔑 {name} NOT provided")
    if kind == "capability.revalidate" and phase == "start":
        return f"     🧪 revalidating remembered tool {name} (stamped: {data.get('stamped')})"
    if kind == "capability.quarantined":
        return f"     ☣ {name} QUARANTINED: {str(data.get('reason', ''))[:80]} — re-authoring"
    if kind == "capability.unavailable":
        return f"     ⛔ tool {name} unavailable this session (needs {data.get('needs')})"
    if kind == "agent.tool_call":
        short = name.split("__")[-1]
        prev = data.get("args_preview")
        return f"       ⚒ {short}({prev})" if prev else f"       ⚒ {short}"
    return None


_HELP = """commands:
  <text>        run an objective (watch it work live)
                mention files inline with @path (e.g. @data/inbox/report.pdf)
  /attach <p>   attach a file for ALL following objectives (/detach to remove)
  /files        list attached files
  /runs         list recent runs
  /show [id]    print a run's timeline (default: last)
  /caps         list the capability library
  /last         re-print the last answer
  /help         this help
  /quit, /exit  leave (Ctrl-D also works)"""


async def _heartbeat(state: dict, stall_limit: float) -> None:
    """Print a liveness line during silent stretches so 'thinking' is visibly
    different from 'stuck' (true stalls are killed by the session watchdog)."""
    last_print = 0.0
    while True:
        await asyncio.sleep(5)
        now = time.monotonic()
        quiet = now - state["ts"]
        if quiet >= 25 and now - last_print >= 25:
            print(f"  … still working — {int(quiet)}s since last activity "
                  f"(last: {state['label']}; agent sessions abort after {int(stall_limit)}s of "
                  "true silence — long quiet here can be builds or generation)", flush=True)
            last_print = now


async def _repl() -> int:
    from . import config
    from .observability import view
    from .session import BrainSession

    print("The Project Brain — interactive session. Type an objective, or /help.\n")
    session = BrainSession(auto_export=True)
    state = {"ts": time.monotonic(), "label": "idle"}

    def _on_event(rec: dict) -> None:
        state["ts"] = time.monotonic()
        state["label"] = f"{rec.get('kind')}/{rec.get('name')}"
        line = format_event(rec)
        if line:
            print(line, flush=True)

    session.add_listener(_on_event)
    last_answer = ""
    attached: list[str] = []           # session-scoped user file set
    try:
        while True:
            try:
                raw = input("brain> ").strip()
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print("  (^C — type /quit to exit)")
                continue
            if not raw:
                continue
            if raw in ("/quit", "/exit"):
                break
            if raw == "/help":
                print(_HELP)
                continue
            if raw == "/runs":
                print(view.render_list(config.DB_PATH, limit=15))
                continue
            if raw.startswith("/show"):
                parts = raw.split()
                rid = parts[1] if len(parts) > 1 and parts[1] != "last" else view.last_run_id(config.DB_PATH)
                print(view.render_run(config.DB_PATH, rid) if rid else "(no runs yet)")
                continue
            if raw == "/caps":
                for c in session.registry.all():
                    print(f"  {c.name:<30} {'✓' if c.verified else '·'}  {c.description[:60]}")
                continue
            if raw == "/last":
                print(last_answer or "(nothing yet)")
                continue
            if raw.startswith("/attach"):
                p = raw.split(None, 1)[1].strip() if " " in raw else ""
                from . import config as _cfg
                if p and (_cfg.PROJECT_ROOT / p).is_file():
                    attached.append(p)
                    print(f"  📎 attached: {p}")
                else:
                    print(f"  file not found: {p or '(missing path)'} — put it under the "
                          "project dir (e.g. data/inbox/) and use a relative path")
                continue
            if raw.startswith("/detach"):
                p = raw.split(None, 1)[1].strip() if " " in raw else "all"
                attached[:] = [] if p == "all" else [a for a in attached if a != p]
                print(f"  attached files: {attached or '(none)'}")
                continue
            if raw == "/files":
                print(f"  attached files: {attached or '(none)'}")
                continue
            if raw.startswith("/"):
                print(f"unknown command: {raw} — try /help")
                continue

            # An objective. Ctrl-C cancels THIS objective, not the session.
            print(f"\n⟳ working on: {raw}   (Ctrl-C cancels this objective)")
            state["ts"] = time.monotonic()
            state["label"] = "planning"
            loop = asyncio.get_running_loop()
            task = asyncio.create_task(session.run_objective(raw, files=list(attached)))
            hb = asyncio.create_task(_heartbeat(state, config.AGENT_STALL_TIMEOUT))
            sig_installed = True
            try:
                loop.add_signal_handler(signal.SIGINT, task.cancel)
            except (NotImplementedError, ValueError):
                sig_installed = False
            try:
                summary = await task
            except asyncio.CancelledError:
                print("\n⛔ cancelled — run marked aborted, containers reaped.")
                continue
            finally:
                hb.cancel()
                if sig_installed:
                    try:
                        loop.remove_signal_handler(signal.SIGINT)
                    except Exception:  # noqa: BLE001
                        pass
            last_answer = summary.answer
            print(f"\n=== {summary.status.upper()} "
                  f"(run {summary.run_id}, {summary.containers} containers, {summary.leaks} leaks) ===")
            print(summary.answer)
            print()
    finally:
        session.close()
    print("bye.")
    return 0


def main() -> None:
    import sys

    try:
        sys.exit(asyncio.run(_repl()))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
