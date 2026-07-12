"""User-provided files: @-mention parsing and workspace materialization.

Generic by design: the engine only DELIVERS files into the run's workspace and
tells the brain they exist (names/sizes — never contents). How a file is
opened, whether it was read correctly, and what extra processing it needs is
the brain's own reasoning: its planner designs the stages, and it authors
whatever file-handling capability the format demands.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import config

# @"path with spaces" or @bare/path (stops at whitespace)
_MENTION = re.compile(r'@(?:"([^"]+)"|([^\s"\'@]+))')

# A path/filename token, backticked (`data/inbox/x.mp4`) or bare (foo.mp4).
_PATHLIKE = re.compile(r'`([^`]+)`|([\w./-]+\.\w{1,6})')


def parse_mentions(text: str) -> tuple[str, list[str]]:
    """Extract @file mentions; rewrite each to its workspace name (input/<name>)
    so the objective the agents see references the path they can actually use."""
    paths: list[str] = []

    def _sub(m: re.Match) -> str:
        p = m.group(1) or m.group(2)
        trailer = ""
        if m.group(2):
            # Mentions live inside sentences: "@x.pdf, summarize" — trailing
            # sentence punctuation is not part of the path. (Quoted mentions
            # take their content verbatim.)
            stripped = p.rstrip(".,;:!?)]}'\"")
            trailer = p[len(stripped):]
            p = stripped
        if not p:
            return m.group(0)  # bare '@,' etc. — not a mention
        paths.append(p)
        return f"input/{Path(p).name}{trailer}"

    return _MENTION.sub(_sub, text), paths


def rewrite_inbox_mentions(text: str) -> tuple[str, list[str]]:
    """Recognize files the user DROPPED in the sanctioned inbox and named in the
    objective by path (``data/inbox/x.mp4`` or bare ``x.mp4``) — a first-class
    way to designate a source, alongside @mentions and remote locators. Each
    match is rewritten to its workspace path (``input/<name>``) and returned for
    materialization. ONLY files that actually resolve inside the inbox are
    touched, so an arbitrary host path in objective text is never materialized.
    Run AFTER parse_mentions so already-rewritten ``input/...`` tokens are left
    alone."""
    inbox = config.INBOX_DIR
    if not inbox.is_dir():
        return text, []
    inbox_r = inbox.resolve()
    paths: list[str] = []

    def _sub(m: re.Match) -> str:
        tok = (m.group(1) or m.group(2) or "").strip().strip('\'"')
        if not tok or tok.startswith("input/"):
            return m.group(0)
        for cand in ((config.PROJECT_ROOT / tok), (inbox / Path(tok).name)):
            try:
                c = cand.resolve()
            except (OSError, ValueError):
                continue
            if c.is_file() and str(c).startswith(str(inbox_r)):
                paths.append(str(c))
                repl = f"input/{Path(tok).name}"
                # Keep surrounding backticks; replace only the inner token.
                return m.group(0).replace(tok, repl) if m.group(1) else repl
        return m.group(0)

    return _PATHLIKE.sub(_sub, text), paths


def materialize(paths: list[str], workspace: Path) -> list[dict]:
    """Copy user files into <workspace>/input/ (size-capped, collision-safe).

    Returns a manifest [{'path': 'input/x.pdf', 'bytes': N}]. Raises ValueError
    with a user-facing message for missing files or blown caps."""
    per_cap = config.FILE_ATTACH_MAX_MB * 1024 * 1024
    total_cap = config.FILE_ATTACH_TOTAL_MAX_MB * 1024 * 1024
    dest_dir = workspace / "input"
    manifest: list[dict] = []
    total = 0
    for raw in paths:
        src = (config.PROJECT_ROOT / raw).resolve() if not Path(raw).is_absolute() else Path(raw)
        if not src.is_file():
            raise ValueError(f"attached file not found: {raw}")
        size = src.stat().st_size
        if size > per_cap:
            raise ValueError(f"{raw} is {size // 2**20} MB (cap {config.FILE_ATTACH_MAX_MB} MB)")
        total += size
        if total > total_cap:
            raise ValueError(f"attached files exceed {config.FILE_ATTACH_TOTAL_MAX_MB} MB total")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        n = 2
        while dest.exists() and dest.read_bytes() != src.read_bytes():
            dest = dest_dir / f"{src.stem}-{n}{src.suffix}"
            n += 1
        dest.write_bytes(src.read_bytes())
        manifest.append({"path": f"input/{dest.name}", "bytes": size})
    return manifest


def manifest_note(manifest: list[dict]) -> str:
    """The block appended to the objective so planner, agents, and supervisor
    all see the same facts about what the user provided."""
    lines = [f"- {m['path']} ({m['bytes'] / 2**20:.1f} MB)" for m in manifest]
    return (
        "\n\nUSER-PROVIDED FILES (already in the run workspace; file-handling tools "
        "receive them via their `files` argument):\n" + "\n".join(lines)
    )
