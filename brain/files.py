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
# An output verb IMMEDIATELY before a filename ('produce submission.csv',
# 'write report.md') marks it as a thing to PRODUCE, not read.
_IMMEDIATE_OUTPUT = re.compile(
    r"(?i)\b(produc\w*|deliver\w*|writ\w*|sav\w*|output\w*|generat\w*|creat\w*|"
    r"return\w*|export\w*|nam\w*|call\w*|emit\w*)\W+$")


def _is_output_context(before: str) -> bool:
    """True when the text BEFORE a designated filename marks it as a produced
    DELIVERABLE (so it must not be treated as a missing input): an output verb
    right before it, or a 'deliverable' section nearby. Kept conservative — a
    verb far from the token (e.g. 'produce a chart from data.csv') does NOT
    match, so a real input is still flagged."""
    return bool(_IMMEDIATE_OUTPUT.search(before[-35:])
                or "deliverabl" in before.lower())


def contained(root, ref) -> Optional[Path]:
    """Resolve ``ref`` INSIDE ``root``, or ``None`` when it escapes.

    THE one place the workspace boundary is decided, because a boundary
    maintained by hand at four sites slipped at one of them: the capability
    runtime read its ``files`` argument straight off the agent's tool call and
    joined it to the workspace unguarded, while the three neighbouring paths
    each carried their own copy of the check.

    A join does not contain, it concatenates — ``Path('/ws') / '/proc/self/environ'``
    is ``/proc/self/environ``, so an absolute reference discards the workspace
    entirely and reads whatever the ENGINE can read. Hence: absolute refs and
    ``..`` are refused outright rather than reinterpreted, and the resolved path
    must be *relative to* the root. `str.startswith` is not that test — it also
    accepts a sibling directory sharing the root's name — and resolving first is
    what makes a symlink pointing outside fail here rather than later.

    Refusing is deliberate where normalising would look friendlier: rewriting
    ``/etc/cron.d/pwn`` into ``<root>/etc/cron.d/pwn`` silently ACCEPTS a name
    no honest producer emits. A caller that legitimately tolerates the
    leading-separator spelling (an agent echoing the path it sees) strips it
    itself, in the open, before asking.
    """
    try:
        base = Path(root).resolve()
    except (OSError, ValueError, TypeError):
        return None
    rel = str(ref or "").strip()
    if not rel:
        return None
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    try:
        p = (base / candidate).resolve()
    except (OSError, ValueError):
        return None
    return p if p.is_relative_to(base) else None


# A single path segment the engine is willing to build a filename out of.
# Deliberately narrow: identifiers the planner and the capability author emit
# ("extract_frames", "attempt-3-author", "stage1.fix") all fit, and nothing that
# fits can be a directory separator, a parent reference, or a dotfile.
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def is_safe_segment(part) -> bool:
    """True when ``part`` is one ordinary path segment, safe to join blindly."""
    s = str(part or "")
    return bool(_SEGMENT.fullmatch(s)) and ".." not in s


def safe_segment(part, fallback: str) -> str:
    """Coerce ``part`` into one safe path segment, or return ``fallback``.

    For the callers that must keep going (a planner id becomes a filename, and
    a run must not die because a model chose a strange one). Callers that can
    treat a bad segment as an engine bug should test with
    :func:`is_safe_segment` and refuse instead.
    """
    s = str(part or "").strip()
    if is_safe_segment(s):
        return s
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-.")[:64]
    return cleaned if is_safe_segment(cleaned) else fallback


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
    it.

    "Inside the brain's mount" is now ENFORCED, not merely described. It was
    only ever a sentence in this docstring: every candidate was resolved and
    then read if it existed, so a symlink planted in the inbox — the drop folder
    the threat model names as the most likely attacker channel — redirected the
    read anywhere the orchestrator could reach, and a bare absolute path was
    taken as given. Under compose that includes /proc/self/environ, which holds
    CLAUDE_CODE_OAUTH_TOKEN, so this was the file-shaped path into the workspace
    that SECURITY_MODEL.md claimed did not exist.
    """
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

    roots = []
    for root in (config.PROJECT_ROOT, config.INBOX_DIR):
        try:
            roots.append(Path(root).resolve())
        except (OSError, ValueError):
            pass

    for c in candidates:
        try:
            r = c.resolve()
        except (OSError, ValueError):
            continue
        # Resolved FIRST, judged second: a symlink is followed before the
        # boundary is applied, so it cannot be used to step over it.
        if not any(r == root or r.is_relative_to(root) for root in roots):
            continue
        if r.is_file():
            return r
    return None


def resolve_local_sources(text: str) -> tuple[str, list[str], list[str], list[str]]:
    """Detect files the user DESIGNATED by path in the objective (backticked or
    bare, e.g. ``data/inbox/clip.mp4`` or ``report.pdf``) — a first-class way to
    provide a source, alongside @mentions and remote locators.

    Returns ``(rewritten_text, resolved, unresolved, guessed)``:
      * a token that resolves to a readable file is rewritten in-text to
        ``input/<name>`` and its absolute path returned for materialization;
      * a token that names a file but does NOT resolve is returned unresolved,
        so the caller can ASK for an accessible path instead of letting the run
        author a doomed, file-starved pipeline.

      * an unmarked multi-segment token that resolves to nothing is returned
        as ``guessed``: `name.ext` is equally the shape of a HOSTNAME, and the
        engine states its guess as a fact rather than interrogating the user
        about a file only it believed was named.

    Only file-shaped tokens are considered; URLs and ``input/`` tokens are left
    untouched. Run AFTER parse_mentions."""
    resolved: list[str] = []
    unresolved: list[str] = []
    guessed: list[str] = []

    # Pre-scan: a filename in an output/deliverable context ANYWHERE in the
    # objective is a produced deliverable — its OTHER mentions ('the pipeline
    # regenerates submission.csv') must not be read as missing inputs either.
    output_names: set[str] = set()
    for m in _DESIGNATED.finditer(text):
        raw = m.group(1) if m.group(1) is not None else (m.group(2) or "")
        tok = raw.strip().strip('\'"').rstrip(".,;:!?)]}")
        if _looks_like_file(tok) and _is_output_context(text[max(0, m.start() - 200):m.start()]):
            output_names.add(tok.rsplit("/", 1)[-1].lower())

    def _sub(m: re.Match) -> str:
        explicit = m.group(1) is not None          # backticked: the user marked it
        raw = m.group(1) if explicit else (m.group(2) or "")
        tok = raw.strip().strip('\'"').rstrip(".,;:!?)]}")
        if not _looks_like_file(tok):
            return m.group(0)
        hit = resolve_file(tok)
        if hit is not None:
            if str(hit) not in resolved:
                resolved.append(str(hit))
            return m.group(0).replace(tok, f"input/{hit.name}")
        # Everything below flags the token for the "give me an accessible path"
        # gate, so it must only fire on something the user actually designated.
        # `name.ext` is equally the shape of a HOSTNAME — every TLD matches the
        # file-extension rule — so an objective naming `shop.example.com`
        # bought two interrogations for a path that never existed and then told
        # every agent to report a fatal blocker on it.
        #
        # SHAPE CANNOT DECIDE THIS. `archive.tar.gz` and `shop.example.com` are
        # the same shape, and separating them needs a public-suffix list — a
        # moving external dataset this engine should not carry. So the shape is
        # not asked to decide; the CONSEQUENCE is made proportional instead.
        #
        # A multi-segment unmarked token goes to `guessed`, not `unresolved`:
        # the caller states it as a fact the agents can weigh and does NOT
        # interrogate the user or instruct a fatal blocker on it. Marked
        # designations — backticks, @, a path separator — keep the full gate,
        # and a single-segment `data.csv` is a filename by any reading and keeps
        # it too. What the engine GUESSED is separated from what the user SAID.
        if (not explicit and not any(sep in tok for sep in ("/", "\\"))
                and tok.count(".") > 1):
            if tok not in guessed:
                guessed.append(tok)
            return m.group(0)
        # A filename the objective asks the system to PRODUCE (a deliverable) is
        # NOT a missing INPUT: flagging it as unresolved makes the engine tell
        # the agent to fatal-block on a file it is supposed to CREATE. Skip any
        # token that is a deliverable here OR anywhere in the objective.
        if (tok.rsplit("/", 1)[-1].lower() in output_names
                or _is_output_context(text[max(0, m.start() - 200):m.start()])):
            return m.group(0)
        if tok not in unresolved:
            unresolved.append(tok)
        return m.group(0)

    return _DESIGNATED.sub(_sub, text), resolved, unresolved, guessed


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
