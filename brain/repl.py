"""Interactive terminal for the brain:  python -m brain.repl

Type an objective and watch the brain work live — plan, agents, tool authoring,
verification, answer — all streamed as it happens and recorded under data/.
Commands: /help /runs /show /caps /last /quit
"""
from __future__ import annotations

import asyncio
import signal
import time
from typing import Callable, Optional

try:
    # GNU readline (>=8.1 has bracketed paste ON by default): a multi-line
    # PASTE lands in one edit buffer — newlines included — and only a real
    # Enter submits. Without this, plain input() submitted at the first
    # pasted newline, so long objectives had to be flattened to one line.
    # Also gives history and line editing for free. Best-effort import:
    # environments without a readline module just keep the old behavior
    # (the triple-quote block below still works everywhere).
    import readline  # noqa: F401
except ImportError:  # pragma: no cover — platform without readline
    pass

# The live-stream renderer lives in observability (it renders TRACE
# records, not terminal state); re-exported so existing callers and the
# REPL's own use keep one import site.
from .observability.stream import _ICON, format_event  # noqa: F401


def read_objective(first: str, input_fn: Callable[[str], str] = input) -> str:
    """Assemble a possibly multi-line objective from REPL input.

    Two ways to get multiple lines in:
      * paste directly — with readline's bracketed paste the whole paste is
        one buffer (newlines included), returned as-is;
      * open a triple-quoted block: a first line starting with \"\"\" reads
        until a line that is (or ends with) \"\"\" — works in any terminal,
        including ones without bracketed paste.
    """
    if not first.startswith('"""'):
        return first
    body = first[3:]
    stripped_body = body.rstrip()
    if len(stripped_body) >= 3 and stripped_body.endswith('"""'):
        return stripped_body[:-3].strip()      # one-line form: """text"""
    lines = [body] if body else []
    while True:
        try:
            nxt = input_fn("  ... ")
        except EOFError:
            break
        stripped = nxt.rstrip()
        if stripped == '"""':
            break
        if stripped.endswith('"""'):
            lines.append(stripped[:-3])
            break
        lines.append(nxt)
    return "\n".join(lines).strip()


_HELP = """commands:
  <text>        run an objective (watch it work live)
                mention files inline with @path (e.g. @data/inbox/report.pdf)
                multi-line: paste directly (kept as one objective), or type
                \"\"\" then your lines, then \"\"\" on its own line
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
    last_label = ""
    while True:
        await asyncio.sleep(5)
        now = time.monotonic()
        quiet = now - state["ts"]
        # Suppress repetition (#23): same phase re-announces only at 60s+
        # intervals; a phase CHANGE prints promptly.
        interval = 25 if state["label"] != last_label else 60
        if quiet >= 25 and now - last_print >= interval:
            print(f"  … {state['label']} — quiet for {int(quiet)}s "
                  f"(sessions abort after {int(stall_limit)}s of true silence; "
                  "long quiet = builds or generation)", flush=True)
            last_print = now
            last_label = state["label"]


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
                raw = read_objective(input("brain> ").strip())
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
