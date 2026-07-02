"""Interactive terminal for the brain:  python -m brain.repl

Type an objective and watch the brain work live — plan, agents, tool authoring,
verification, answer — all streamed as it happens and recorded under data/.
Commands: /help /runs /show /caps /last /quit
"""
from __future__ import annotations

import asyncio
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
        agents = data.get("agents") or []
        roles = ", ".join(a.get("id", "?") for a in agents)
        return f"  📋 plan: {len(agents)} agent(s) — {roles}"
    if kind == "agent.created":
        caps = data.get("capabilities") or []
        cap_s = f" [{', '.join(caps)}]" if caps else " [no tools — reasons unaided]"
        return f"  🤖 agent ▸ {data.get('role', name)}{cap_s}"
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
    if kind == "agent.tool_call":
        return None  # too chatty for the live view
    return None


_HELP = """commands:
  <text>        run an objective (watch it work live)
  /runs         list recent runs
  /show [id]    print a run's timeline (default: last)
  /caps         list the capability library
  /last         re-print the last answer
  /help         this help
  /quit, /exit  leave (Ctrl-D also works)"""


async def _repl() -> int:
    from . import config
    from .observability import view
    from .session import BrainSession

    print("The Project Brain — interactive session. Type an objective, or /help.\n")
    session = BrainSession(auto_export=True)
    session.add_listener(lambda rec: (line := format_event(rec)) and print(line, flush=True))
    last_answer = ""
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
            if raw.startswith("/"):
                print(f"unknown command: {raw} — try /help")
                continue

            # An objective.
            print(f"\n⟳ working on: {raw}")
            summary = await session.run_objective(raw)
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
