"""Interactive terminal for the brain:  python -m brain.repl

Say something and watch the brain work live — analysis, plan, agents, tool
authoring, verification, answer — streamed as it happens and recorded under
data/. Each process is one conversation, resumable from the record.
Commands: /help /runs /show /convs /conv /resume /caps /last /quit
"""
from __future__ import annotations

import asyncio
import signal
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
from .observability.ticker import Ticker


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
  <text>        say something (watch it work live)
                mention files inline with @path (e.g. @data/inbox/report.pdf)
                multi-line: paste directly (kept as one objective), or type
                \"\"\" then your lines, then \"\"\" on its own line
  /attach <p>   attach a file for ALL following objectives (/detach to remove)
  /files        list attached files
  /runs         list recent runs
  /show [id]    print a run's timeline (default: last)
  /convs        list recorded conversations
  /conv [id]    print a conversation's turns (default: this one)
  /resume <id>  continue a recorded conversation (id, unique prefix, or 'last')
  /caps         list the capability library
  /last         re-print the last answer
  /help         this help
  /quit, /exit  leave (Ctrl-D also works)"""


async def _repl(resume: Optional[str] = None) -> int:
    from . import config
    from . import conversation as conversation_mod
    from .observability import view
    from .session import BrainSession

    print("The Project Brain — interactive session. Say something, or /help.\n")
    if resume:
        resolved = conversation_mod.resolve(config.DB_PATH, resume)
        if resolved is None:
            print(f"no conversation matches {resume!r} — starting a new one "
                  "(/convs lists what is recorded)")
        resume = resolved
    session = BrainSession(auto_export=True, resume=resume)
    print(f"conversation: {session.conversation_id}\n")
    ticker = Ticker(output_fn=lambda s: print(s, flush=True),
                    stall_limit=config.AGENT_STALL_TIMEOUT)

    def _on_event(rec: dict) -> None:
        ticker.observe(rec)
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
            if raw.startswith("/convs"):
                print(view.render_conversations(config.DB_PATH, limit=15))
                continue
            if raw.startswith("/conv"):
                ref = raw.split(None, 1)[1].strip() if " " in raw else session.conversation_id
                cid = conversation_mod.resolve(config.DB_PATH, ref) or ref
                print(view.render_conversation(config.DB_PATH, cid))
                continue
            if raw.startswith("/resume"):
                ref = raw.split(None, 1)[1].strip() if " " in raw else ""
                cid = conversation_mod.resolve(config.DB_PATH, ref) if ref else None
                if cid is None:
                    print(f"no conversation matches {ref or '(missing id)'!r} — "
                          "/convs lists what is recorded")
                elif session.switch_conversation(cid):
                    last_answer = (session._last_run or {}).get("deliverable", "")
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
            ticker.reset()
            loop = asyncio.get_running_loop()
            task = asyncio.create_task(session.run_objective(raw, files=list(attached)))
            hb = asyncio.create_task(ticker.loop())
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
            # What this turn cost, and the conversation so far. A system whose
            # turns span cents to double-digit dollars must not ask a person to
            # converse with it blind — and until now only the one-shot path
            # printed anything at all.
            from .observability.cost import turn_cost_line

            if line := turn_cost_line(config.DB_PATH, summary.run_id,
                                      session.conversation_id):
                print(line)
            print()
    finally:
        session.close()
    print("bye.")
    return 0


def main() -> None:
    import sys

    args = sys.argv[1:]
    resume: Optional[str] = None
    if args and args[0] in ("--resume", "-r"):
        resume = args[1] if len(args) > 1 else "last"
    elif args:
        print("usage: python -m brain.repl [--resume <conversation_id|prefix|last>]")
        sys.exit(2)
    try:
        sys.exit(asyncio.run(_repl(resume)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
