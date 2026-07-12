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
from typing import Optional

from . import config

# @"path with spaces" or @bare/path (stops at whitespace)
_MENTION = re.compile(r'@(?:"([^"]+)"|([^\s"\'@]+))')

# A DESIGNATED file the user named in the objective: a backticked token, or a
# bare token ending in a file extension. The extension must START with a letter
# then be alphanumeric (so ".mp4"/".mp3"/".h264" match, while "e.g", "i.e", and
# "v3.12" — digit- or single-char tails — do not).
_DESIGNATED = re.compile(r'`([^`]+)`|([^\s`"\'<>|]+\.[A-Za-z][A-Za-z0-9]{1,5})\b')
_HAS_EXT = re.compile(r'\.[A-Za-z][A-Za-z0-9]{1,5}$')


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


def _looks_like_file(tok: str) -> bool:
    """A token that plausibly names a file the user wants used — not a URL, not
    an already-rewritten workspace path, ending in a real file extension."""
    tok = tok.strip().strip('\'"`').rstrip(".,;:!?)]}")
    if not tok or "://" in tok or tok.lower().startswith(("www.", "input/")):
        return False
    return _HAS_EXT.search(tok) is not None


def resolve_file(token: str) -> Optional[Path]:
    """Resolve a user-named path to a readable file the ENGINE can reach —
    project-relative, absolute (inside the brain's mount), or a basename in the
    sanctioned inbox. Returns None when nothing accessible matches. This is the
    heart of "why can't the agent read the file I named": a sandboxed tool only
    ever sees files the engine copies into the workspace, so a path the engine
    cannot resolve here can never reach a tool, no matter how the agent spells
    it."""
    tok = token.strip().strip('\'"`').rstrip(".,;:!?)]}")
    if not tok:
        return None
    candidates = []
    p = Path(tok)
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(config.PROJECT_ROOT / tok)
        try:
            candidates.append(Path.cwd() / tok)
        except OSError:
            pass
    candidates.append(config.INBOX_DIR / Path(tok).name)
    for c in candidates:
        try:
            r = c.resolve()
        except (OSError, ValueError):
            continue
        if r.is_file():
            return r
    return None


def resolve_local_sources(text: str) -> tuple[str, list[str], list[str]]:
    """Detect files the user DESIGNATED by path in the objective (backticked or
    bare, e.g. ``data/inbox/clip.mp4`` or ``report.pdf``) — a first-class way to
    provide a source, alongside @mentions and remote locators.

    Returns ``(rewritten_text, resolved_abs_paths, unresolved_tokens)``:
      * a token that resolves to a readable file is rewritten in-text to
        ``input/<name>`` and its absolute path returned for materialization;
      * a token that clearly names a file but does NOT resolve is returned
        unresolved, so the caller can ASK the user for an accessible path
        instead of letting the run author a doomed, file-starved pipeline.

    Only file-shaped tokens are considered; URLs and ``input/`` tokens are left
    untouched. Run AFTER parse_mentions."""
    resolved: list[str] = []
    unresolved: list[str] = []

    def _sub(m: re.Match) -> str:
        raw = m.group(1) if m.group(1) is not None else (m.group(2) or "")
        tok = raw.strip().strip('\'"').rstrip(".,;:!?)]}")
        if not _looks_like_file(tok):
            return m.group(0)
        hit = resolve_file(tok)
        if hit is not None:
            if str(hit) not in resolved:
                resolved.append(str(hit))
            return m.group(0).replace(tok, f"input/{hit.name}")
        if tok not in unresolved:
            unresolved.append(tok)
        return m.group(0)

    return _DESIGNATED.sub(_sub, text), resolved, unresolved


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
