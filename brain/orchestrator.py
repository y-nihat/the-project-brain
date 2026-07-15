"""Executor / orchestrator: objective -> staged workflow -> run the stages.

The planner designs a DAG of stages (single | map | reduce | review); the
orchestrator executes them in dependency order:

  * ``single``  — one agent session (the degenerate workflow; simple tasks stay
    exactly as cheap as before);
  * ``map``     — fan-out decided at RUN time: the upstream stage's manifest is
    sharded and one worker session runs per shard, in bounded parallel
    ("constrain locally, aggregate globally");
  * ``reduce``  — consolidates the workers' notes, hierarchically when the
    volume exceeds one context;
  * ``review``  — evidence-grounded critic: checks the draft's claims against
    the underlying notes and demands fixes for a bounded number of rounds.

Agents never spawn agents; all width and depth decisions happen here, in code,
under hard caps. Every session keeps the existing safety rails (watchdog, call
budget, health monitor + runtime tool repair).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from pathlib import Path
from typing import Awaitable, Callable, Optional

from . import config, factory, workflow
from . import criteria as criteria_mod
from . import evidence as evidence_mod
from .acquisition import (AcquisitionError, acquire, generalize_description,
                          make_builder_agent)
from .acquisition import repair_capability as acquire_repair
from .capabilities.runtime import build_caps_server
from .models import (CREATE, MAP, REDUCE, REUSE, REVIEW, AgentSpec, Capability,
                     Decision, MethodOutcome, StageSpec)
from .team import TEAM_TOOLS, TeamChannel, build_team_server

_VERDICT_RE = re.compile(r"VERDICT:\s*(approved|revise|needs-evidence)", re.IGNORECASE)
# Audience-perceivable artifact formats that need RENDERING before anyone can
# look at them (images are directly perceivable via read_file). Modality
# categories, not task types — same standing as the image-extension set.
_RENDERABLE_EXTS = {".pptx", ".ppt", ".odp", ".docx", ".doc", ".odt", ".pdf",
                    ".html", ".htm", ".svg", ".rtf"}
_RENDER_NEED_KEY = "render_artifact_pages"
_RENDER_CONTRACT = (
    "Accepts a document/presentation/page-layout file via the files argument. Renders "
    "EVERY page/slide/frame of it to sequentially numbered PNG images (one image per "
    "page, at a resolution where body text is readable) in the output directory. "
    "Returns page_count and the list of image filenames, reports consumed_inputs, and "
    "fails honestly when the format cannot be rendered. Purely a converter — no content "
    "interpretation, no editing, no summarizing.")
_CRITERIA_BLOCK_RE = re.compile(r"```criteria\s*\n(.*?)```", re.DOTALL)
# A remote locator in the objective is user-referenced evidence, exactly like an
# attached file (defined in the evidence module; re-exported for callers/tests).
_EXTERNAL_LOCATOR = evidence_mod.EXTERNAL_LOCATOR


def _parse_criteria_verdicts(critique: str) -> list[dict]:
    """The reviewer's per-criterion table (fenced ```criteria JSON): each
    criterion carries its own met/unmet/unknown state and the evidence
    reference that settles it — 'the reviewer approves' is a verdict about
    the SET, and was observed papering over an individually failed count."""
    import json as _json

    matches = _CRITERIA_BLOCK_RE.findall(critique or "")
    for raw in reversed(matches):
        try:
            items = _json.loads(raw.strip())
        except ValueError:
            continue
        if isinstance(items, list):
            out = []
            for it in items:
                if isinstance(it, dict) and it.get("criterion"):
                    status = str(it.get("status", "unknown")).lower()
                    out.append({"criterion": str(it["criterion"])[:300],
                                "status": status if status in ("met", "unmet", "unknown")
                                else "unknown",
                                "evidence": str(it.get("evidence", ""))[:300]})
            return out
    return []


_CLAIMS_BLOCK_RE = re.compile(r"```claims\s*\n(.*?)```", re.DOTALL)
_CLAIM_SUPPORT = ("direct", "derived", "inferred", "negative", "proxy", "unsupported")


def _parse_claims(critique: str) -> list[dict]:
    """The reviewer's claim-support table (fenced ```claims JSON): each
    load-bearing claim typed by HOW it is supported — direct observation,
    target-derived transformation, inference between observations, negative
    (absence), proxy testimony, or unsupported — with the evidence file that
    carries it. The gate checks this table deterministically; an unknown
    support type coerces to 'unsupported' (fail closed, never open)."""
    import json as _json

    matches = _CLAIMS_BLOCK_RE.findall(critique or "")
    for raw in reversed(matches):
        try:
            items = _json.loads(raw.strip())
        except ValueError:
            continue
        if isinstance(items, list):
            out = []
            for it in items:
                if isinstance(it, dict) and it.get("claim"):
                    support = str(it.get("support", "")).strip().lower()
                    out.append({"claim": str(it["claim"])[:300],
                                "support": support if support in _CLAIM_SUPPORT
                                else "unsupported",
                                "evidence": str(it.get("evidence", ""))[:300]})
            return out
    return []


def _claims_gate(claims: list[dict], lineage) -> str:
    """Deterministic claim-support check for an approval on a source-derived
    run. Returns a void reason, or '' when the table passes. Rules:

      * the table must exist — an approval without typed claim support is the
        hollow-certificate failure;
      * no claim may be 'unsupported';
      * at least one claim must be supported 'direct' or 'derived' — a
        deliverable resting entirely on inference and proxy testimony cannot
        be certified as grounded in the source;
      * every 'direct'/'derived'/'negative' claim must cite an evidence file
        that actually resolves to a TARGET-LINEAGE ledger entry (matched by
        path or basename) — a citation the engine cannot resolve is a
        miscitation, not support.
    """
    if not claims:
        return ("approval without a claim-support table: emit the ```claims block "
                "typing each load-bearing claim and the target-lineage file that "
                "supports it")
    unsupported = [c for c in claims if c["support"] == "unsupported"]
    if unsupported:
        return (f"unsupported load-bearing claim: {unsupported[0]['claim'][:120]!r} — "
                "revise the deliverable, qualify the claim, or obtain evidence")
    grounded = [c for c in claims if c["support"] in ("direct", "derived")]
    if not grounded:
        return ("no claim is supported by target-lineage evidence: everything rests on "
                "inference or proxy testimony, which cannot certify claims about the "
                "designated source")
    paths = {e.file for e in lineage}
    basenames = {Path(e.file).name for e in lineage}
    for c in claims:
        if c["support"] not in ("direct", "derived", "negative"):
            continue
        cited = c.get("evidence", "").strip()
        hit = any(p in cited for p in paths) or \
            any(b and b in cited for b in basenames)
        if not hit:
            return (f"miscited support: claim {c['claim'][:100]!r} is typed "
                    f"'{c['support']}' but its citation {cited[:120]!r} does not resolve "
                    "to any target-lineage ledger file")
    return ""


# Gate defects that are TABLE MECHANICS — re-emittable without re-judging the
# draft: a missing table, or a citation string that fails to resolve to a
# lineage file the reviewer actually verified. Substantive defects (an
# unsupported claim, no grounded claim at all) are deliberately absent:
# inviting a retype under void pressure would be a laundering channel.
_FIXABLE_GATE_PREFIXES = ("approval without a claim-support table",
                          "miscited support")


def _failure_signature(text: str) -> str:
    """Normalize an error detail so REPEATS of the same failure are countable
    across attempts and across differently-named contracts: hex ids, numbers
    and whitespace vary run to run; the failure shape does not."""
    s = re.sub(r"[0-9a-f]{8,}", "<hex>", str(text or "").lower())
    s = re.sub(r"\d+", "<n>", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:160]


def _stage_dict(s: StageSpec) -> dict:
    return {"id": s.id, "pattern": s.pattern, "role": s.role, "goal": s.goal,
            "capabilities": s.capabilities, "depends_on": s.depends_on,
            "shard_size": s.shard_size, "shard_from": s.shard_from, "rounds": s.rounds,
            "expectation": s.expectation, "deliverable": s.deliverable}


def _persist_agent(runs_dir, session: str, spec: AgentSpec, system_prompt: str, output: str) -> None:
    """Agent outputs are first-class data-layer files, not just DB rows."""
    base = Path(runs_dir) / session
    outputs = base / "outputs"
    agents = base / "agents"
    outputs.mkdir(parents=True, exist_ok=True)
    agents.mkdir(parents=True, exist_ok=True)
    header = (
        f"# {spec.id} — {spec.role}\n\n"
        f"- **goal:** {spec.goal}\n"
        f"- **capabilities:** {spec.capabilities}\n"
        f"- **depends_on:** {spec.depends_on}\n\n---\n\n"
    )
    (outputs / f"{spec.id}.md").write_text(header + (output or ""))
    (agents / f"{spec.id}.prompt.md").write_text(system_prompt)


def _seed_prior_workspace(runs_dir, session: str, seed_run: str, *, max_bytes: int = 16 * 1024 * 1024) -> list[str]:
    """Copy a prior run's workspace files under `prior/<run_id>/` (bounded).

    The prior run's own `prior/` subtree is skipped — otherwise seeds nest
    recursively, run after run.
    """
    src = Path(runs_dir) / seed_run / "workspace"
    dst = Path(runs_dir) / session / "workspace" / "prior" / seed_run
    copied: list[str] = []
    total = 0
    if not src.is_dir():
        return copied
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if rel.parts and rel.parts[0] == "prior":
            continue
        size = p.stat().st_size
        if total + size > max_bytes:
            break
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(p.read_bytes())
        total += size
        copied.append(f"prior/{seed_run}/{rel}")
    return copied


def _workspace_files(root: Path) -> list[str]:
    """Every file the run holds, sorted, workspace-relative — `prior/` seeds
    INCLUDED. Hiding seeded files from enumeration taught an agent its
    workspace was empty while every file it needed sat under `prior/`."""
    root = Path(root).resolve()
    return sorted(str(x.relative_to(root)).replace("\\", "/")
                  for x in root.rglob("*") if x.is_file())


def _workspace_rollup(files: list[str], max_dirs: int = 24) -> list[str]:
    """One line per directory: file count plus FIRST and LAST member names, so
    a numbered sequence reveals its full range without enumerating every
    member (a first-N-of-M cut hides exactly the far end an agent asks about)."""
    groups: dict[str, list[str]] = {}
    for f in files:
        d, _, name = f.rpartition("/")
        groups.setdefault(d, []).append(name)
    lines: list[str] = []
    for d in sorted(groups):
        names = groups[d]
        label = f"{d}/" if d else "(workspace root)"
        if len(names) <= 3:
            lines.append(f"- {label}: {', '.join(names)}")
        else:
            lines.append(f"- {label}: {len(names)} files, {names[0]} … {names[-1]}")
    if len(lines) > max_dirs:
        rest = len(lines) - max_dirs
        lines = lines[:max_dirs] + [f"- (+{rest} more directories — use list_files)"]
    return lines


def _missing_file_error(root: Path, rel: str) -> str:
    """The read_file miss message. It must CORRECT the agent, not mislead it:
    nearest candidates for the requested name first (a wrong directory prefix
    was the most common miss), then a rollup of what the run actually holds,
    with truncation framed so it cannot ground an absence conclusion."""
    import difflib

    files = _workspace_files(Path(root))
    if not files:
        return (f"no such workspace file: {rel!r} — the workspace holds no "
                "files at all yet.")
    base = (rel or "").replace("\\", "/").rpartition("/")[2]
    hint = ""
    exact = [f for f in files if f.rpartition("/")[2] == base]
    if exact:
        hint = ("NEAREST MATCH — a file with exactly this name exists at: "
                + ", ".join(exact[:8]) + ". ")
    elif base:
        names = sorted({f.rpartition("/")[2] for f in files})
        close = set(difflib.get_close_matches(base, names, n=6, cutoff=0.6))
        similar = [f for f in files if f.rpartition("/")[2] in close]
        if similar:
            hint = "Similarly named files exist: " + ", ".join(similar[:8]) + ". "
    rollup = "\n".join(_workspace_rollup(files))
    return (f"no such workspace file: {rel!r}. {hint}"
            f"The workspace holds {len(files)} file(s), by directory "
            "(counts with first … last names — a SUMMARY, not a full "
            "enumeration; a name's absence from this message is NOT evidence "
            "the file does not exist — enumerate with list_files):\n" + rollup)


def _notes_block(notes: dict[str, str], limit: int) -> str:
    """Render worker notes for a prompt, hard-capped at `limit` chars."""
    parts: list[str] = []
    used = 0
    for wid, text in notes.items():
        chunk = f"\n[{wid}]:\n{text}\n"
        if used + len(chunk) > limit:
            parts.append(f"\n[engine note] remaining notes truncated at {limit} chars.")
            break
        parts.append(chunk)
        used += len(chunk)
    return "".join(parts)


def _batch_notes(notes: dict[str, str], limit: int) -> list[dict[str, str]]:
    """Greedily group notes into batches each fitting the char limit."""
    batches: list[dict[str, str]] = []
    current: dict[str, str] = {}
    used = 0
    for wid, text in notes.items():
        if current and used + len(text) > limit:
            batches.append(current)
            current, used = {}, 0
        current[wid] = text
        used += len(text)
    if current:
        batches.append(current)
    return batches


def _truncation_confidence(frag: str) -> Optional[str]:
    """Visible-text truncation confidence: 'definite', 'suspected', or None.

    'definite' requires STRUCTURAL evidence of a cut — a 1-2 char trailing stub
    ('Trust s'), a 'label: value' value cut short ('Source: analy'), a slashed
    pair cut after the slash ('Violet/t'), or a bracket/quote opened but never
    closed ('…(reactive trigge'). Only definite findings gate repair and
    classification. A lowercase-ending phrase with no terminal punctuation is
    merely 'suspected' — review context, never an automatic repair trigger:
    ordinary captions ('How Claude works', 'Why it matters') look exactly like
    that, and the false 'definite' burned two rewrites and quarantined a
    healthy tool. Headings/titles are excluded by the caller."""
    f = frag.strip()
    if not f or f[-1] in ".!?)]\"'”’:":       # terminal punctuation -> complete
        return None
    if re.search(r"\w{2,}/[A-Za-z]$", f):     # "Violet/t" — pair cut after the slash
        return "definite"
    words = f.split()
    if len(words) < 2:
        return None
    last = words[-1]
    if last.isupper():                        # complete acronym ("AI", "HTML")
        return None
    if len(last) <= 2 and last.isalpha() and last.islower():
        return "definite"                     # "Trust s"
    if re.search(r"[:\-]\s+[a-z]{2,8}$", f):  # "Source: analy"
        return "definite"
    if last[-1].isalpha() and last.islower() and any(
            f.count(o) > f.count(c) for o, c in ("()", "[]", "{}", "“”")):
        return "definite"                     # "…(reactive trigge" — structure left open
    if last.islower() and len(words) >= 4:    # sentence-shaped, may just be a caption
        return "suspected"
    return None


def artifact_issues(workspace: Path, *, definite_only: bool = False) -> list[str]:
    """Generic structured-output hygiene (#16, refined): scan produced text
    artifacts for truncated visible fragments. Headings/titles are excluded (a
    complete title is not truncation — the reported false positive). With
    ``definite_only``, return just the high-confidence defects that should drive
    automatic repair."""
    root = Path(workspace)
    issues: list[str] = []
    if not root.is_dir():
        return issues
    heading = re.compile(r"<(h[1-6])[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
    # CSS/JS/comments are NOT visible text — scanning them produced false
    # "truncation" like 'transform-origin:720px 450px}' that burned repair
    # cycles and regressed a reviewed artifact.
    invisible = re.compile(r"<(style|script)[^>]*>.*?</\1>|<!--.*?-->",
                           re.IGNORECASE | re.DOTALL)
    # Hygiene applies to what an AUDIENCE will read: files whose producing
    # tool declared a non-artifact role (observation/derived/validation) are
    # evidence, not deliverables — a research agent's fetched pages were
    # observed polluting the terminal classification with 'artifact defects'.
    non_artifact: set[str] = set()
    for rec in evidence_mod._records(root.parent):
        role = str(rec.get("role") or "")
        for f in rec.get("files") or []:
            if role and role != "artifact":
                non_artifact.add(str(f))
            else:
                non_artifact.discard(str(f))
    for p in sorted(root.rglob("*")):
        if not p.is_file() or (p.relative_to(root).parts[:1] in (("prior",), ("input",))):
            continue
        if str(p.relative_to(root)) in non_artifact:
            continue
        rel = p.relative_to(root)  # path names the PRODUCING capability's dir —
        try:                       # defects must be attributable to their producer
            text = p.read_text(errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        # A rendered artifact whose <title> is a file path has no real title.
        t = re.search(r"<title[^>]*>([^<]*)</title>", text, re.IGNORECASE)
        if t and re.search(r"[\w-]+/[\w-]+|\.\w{2,4}\b", t.group(1)):
            issues.append(f"{rel} [definite]: <title> is a file path/metadata, not a "
                          f"title — “{t.group(1).strip()[:60]}”")
        body = heading.sub(" ", invisible.sub(" ", text))
        for frag in re.findall(r">([^<>]{12,})<", body):
            f = frag.strip()
            # Markup leaking into RENDERED text: markdown in HTML visible text.
            if re.search(r"\*\*[^*]+\*\*|(?:^|\s)##+\s", f):
                issues.append(f"{rel} [definite]: raw markdown in rendered text — "
                              f"“{f[:45]}”")
                continue
            conf = _truncation_confidence(f)
            if conf == "definite":
                issues.append(f"{rel} [definite]: truncated text — “…{f[-45:]}”")
            elif conf and not definite_only:
                issues.append(f"{rel} [suspected]: possibly cut short (or just an "
                              f"unpunctuated caption — judge in context) — “…{f[-45:]}”")
    return issues[:12]


def _evidence_files(workspace: Path, reader_dirs: tuple,
                    extra_paths: Optional[list] = None) -> list[Path]:
    """The files that count as ORIGINAL evidence for lookup/packet assembly:
    user inputs, observation-role extractions, PLUS target-lineage payloads
    from the ledger (a property-preserving derivative is evidence of the
    source whatever role its producing tool declared)."""
    root = Path(workspace)
    out: list[Path] = []
    seen: set = set()
    for sub in ("input", *reader_dirs):
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and p not in seen:
                seen.add(p)
                out.append(p)
    for rel in extra_paths or []:
        p = root / str(rel)
        if p.is_file() and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# Typography normalization for evidence lookup: sources and drafts spell the
# SAME text differently (curly vs straight quotes, en/em dashes, PDF ligatures,
# markdown emphasis around a quoted phrase) — every variant is one lexeme for
# matching purposes. False NOT-FOUNDs burn review rounds and manufacture doubt:
# a reviewer was observed re-verifying by direct file read what the lookup had
# just denied. Matching stays deterministic; approximate hits are LABELED.
_LOOKUP_CHAR_MAP = {"’": "'", "‘": "'", "‚": "'", "′": "'",
                    "“": '"', "”": '"', "„": '"', "″": '"',
                    "–": "-", "—": "-", "‒": "-", "−": "-",
                    "…": "...", "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff",
                    "­": ""}
_LOOKUP_TRANSPARENT = frozenset("*_`")   # markdown emphasis/code markers


def _flatten_for_lookup(raw: str) -> tuple[str, list[int]]:
    """Whitespace-collapsed, typography-normalized stream plus a map back to
    original character offsets (evidence files wrap mid-sentence, so matching
    runs on the collapsed stream and line numbers are recovered per hit)."""
    flat: list[str] = []
    positions: list[int] = []
    last_ws = True
    for i, ch in enumerate(raw):
        if ch.isspace():
            if not last_ws:
                flat.append(" ")
                positions.append(i)
            last_ws = True
            continue
        mapped = "" if ch in _LOOKUP_TRANSPARENT else _LOOKUP_CHAR_MAP.get(ch, ch)
        for out in mapped:
            flat.append(out)
            positions.append(i)
            last_ws = False
    return "".join(flat), positions


def _locate_evidence(workspace: Path, reader_dirs: tuple, query: str,
                     context: int = 240, max_matches: int = 8,
                     extra_paths: Optional[list] = None) -> str:
    """Deterministic claim lookup over the run's ORIGINAL evidence (user inputs
    + observation-role extractions + target-lineage payloads — never authored
    artifacts, which would let a draft confirm itself). A reviewer must
    adjudicate 'is this number/quote in the source?' by DIRECT LOOKUP, not by
    asking the stage that produced the draft: a tool-less follow-up agent
    answers from its own prose memory and was observed testifying a verbatim
    source figure was 'possibly hallucinated' — a true claim got removed on
    false testimony. Recall discipline: text present in retained evidence MUST
    be findable — matching is typography-normalized, decoding is tolerant (one
    bad byte must not hide a whole file), and when the exact phrase misses, a
    token-level pass reports clearly-labeled approximate matches."""
    q = " ".join(str(query or "").split())
    if len(q) < 3:
        return "query too short — give the exact number/phrase/quote to locate."
    qn = _flatten_for_lookup(q)[0].casefold()
    root = Path(workspace)
    hits: list[str] = []
    corpus: list[tuple] = []   # (path, raw, flat, positions, flat_casefolded)
    for p in _evidence_files(workspace, reader_dirs, extra_paths):
        try:
            raw = p.read_text(errors="replace")
        except OSError:
            continue
        if raw.count("�") > max(8, len(raw) // 20):
            continue   # effectively binary — not a text evidence file
        flat, positions = _flatten_for_lookup(raw)
        corpus.append((p, raw, flat, positions, flat.casefold()))

    def record_hit(p, raw, flat, positions, j: int, span: int) -> None:
        orig = positions[j] if j < len(positions) else 0
        line = raw.count("\n", 0, orig) + 1
        lo, hi = max(0, j - context), min(len(flat), j + span + context)
        snippet = flat[lo:hi].strip()
        hits.append(f"[{p.relative_to(root)}:{line}] …{snippet}…")

    for p, raw, flat, positions, flat_s in corpus:
        if len(hits) >= max_matches:
            break
        start = 0
        while len(hits) < max_matches:
            j = flat_s.find(qn, start)
            if j < 0:
                break
            record_hit(p, raw, flat, positions, j, len(qn))
            start = j + max(1, len(qn))
    if hits:
        return f"FOUND {len(hits)} match(es) for {q!r}:\n" + "\n".join(hits)

    # Token-level fallback: all content tokens, in order, within a bounded
    # window. Reported as APPROXIMATE — 'NOT FOUND' must keep meaning 'not
    # even approximately present', so a reviewer can still trust a miss.
    tokens = [t for t in qn.split() if len(t) >= 3][:12]
    if len(tokens) >= 2:
        window = len(qn) * 2 + 240
        for p, raw, flat, positions, flat_s in corpus:
            if len(hits) >= max_matches:
                break
            start = 0
            while len(hits) < max_matches:
                j = flat_s.find(tokens[0], start)
                if j < 0:
                    break
                pos, ok = j + len(tokens[0]), True
                for t in tokens[1:]:
                    k = flat_s.find(t, pos, j + window)
                    if k < 0:
                        ok = False
                        break
                    pos = k + len(t)
                if ok:
                    record_hit(p, raw, flat, positions, j, pos - j)
                    start = pos
                else:
                    start = j + 1
        if hits:
            return (f"FOUND {len(hits)} APPROXIMATE match(es) for {q!r} — the exact "
                    "phrase does not occur, but all its content words appear "
                    "together, in order (verify the snippet's actual wording "
                    "before citing it as verbatim):\n" + "\n".join(hits))

    return (f"NOT FOUND: {q!r} does not occur in any of the run's evidence files "
            f"(searched {len(corpus)} readable file(s): user inputs, extractions, and "
            "target-lineage payloads; matching was typography-normalized and was "
            "also attempted token-by-token). If a draft asserts it as a source "
            "fact, it is unsupported.")


def _source_evidence(workspace: Path, reader_dirs: tuple = (), limit: int = 6000,
                     extra_paths: Optional[list] = None) -> str:
    """The run's ACTUAL source evidence: user inputs, the outputs of
    reader-class capabilities, and target-lineage payloads from the ledger
    (``extra_paths``) — a property-preserving derivative is source evidence
    whatever role its producing tool declared. Review must trace claims to
    THIS, so a theme hallucinated in an intermediate analysis but absent from
    the source is caught."""
    root = Path(workspace)
    if not root.is_dir():
        return ""
    parts: list[str] = []
    used = 0
    for p in _evidence_files(workspace, reader_dirs, extra_paths):
        try:
            text = p.read_text(errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        # Budget-aware assembly: the header counts INSIDE the budget and an
        # oversize first file is included PARTIALLY, never skipped — the old
        # break-on-overflow returned an EMPTY packet exactly when the
        # extraction was substantial, so review ran with no ground truth.
        header = f"[source: {p.relative_to(root)}]\n"
        room = limit - used - len(header) - 1
        if room < 200:
            return "\n".join(parts)
        body = text[:room]
        if len(text) > room:
            body += f"\n[…evidence continues: {len(text) - room:,} more chars in this file]"
        parts.append(header + body + "\n")
        used += len(header) + len(body) + 2
    return "\n".join(parts)


def _observed_artifacts(workspace: Path, limit: int = 1500) -> str:
    """Read the HEAD and TAIL of files the run actually produced, so a
    reviewer/supervisor judges the real artifact rather than an agent's
    description of it. The tail matters: 'is the file complete / properly
    closed' is a fact the reviewer must be able to OBSERVE, not ask the
    producing agent to confirm about its own work. Generic across modalities —
    text is shown, binaries are noted by size."""
    root = Path(workspace)
    if not root.is_dir():
        return ""
    parts: list[str] = []
    tail_len = max(200, limit // 4)
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in ("prior", "input"):
            continue  # prior-run seeds and user inputs are not THIS run's output
        try:
            text = p.read_text(errors="strict")
        except (UnicodeDecodeError, OSError):
            parts.append(f"[{rel}] ({p.stat().st_size} bytes, binary)")
            continue
        if len(text) <= limit + tail_len:
            parts.append(f"[{rel}] ({p.stat().st_size} bytes, complete):\n{text}")
        else:
            parts.append(
                f"[{rel}] ({p.stat().st_size} bytes; head and tail shown):\n{text[:limit]}"
                f"\n[…{len(text) - limit - tail_len:,} chars omitted…]\n{text[-tail_len:]}")
    return "\n\n".join(parts)


class Orchestrator:
    def __init__(
        self,
        *,
        session: str,
        manager,
        ledger,
        registry,
        blackboard,
        broker,
        planner,
        resolver,
        model: Optional[str] = None,
        use_builder: bool = True,
        tracer=None,
        session_runner=None,
        supervisor=None,
    ):
        self.session = session
        self.manager = manager
        self.ledger = ledger
        self.registry = registry
        self.blackboard = blackboard
        self.broker = broker
        self.planner = planner
        self.resolver = resolver
        self.model = model
        self.use_builder = use_builder
        from .observability import NullTracer

        self.tracer = tracer or NullTracer()
        # Objective-success class (#17) — starts UNASSESSED, never implicitly
        # successful; only `_classify` (the one authoritative state machine)
        # assigns a terminal value.
        self.classification = "unassessed"
        # Test seam: run an agent session; defaults to observability.trace_query.
        self._session_runner = session_runner
        # Executive judgment (None => no supervision; the session wires one in).
        self.supervisor = supervisor
        self._sessions = 0
        self._repair_stalled = False
        self._verbatim_caps: dict[str, bool] = {}
        self._replans = 0
        self._gaps_seen = 0
        self._criteria: list[str] = []
        self._review_verdicts: list[str] = []
        self._failures: list[MethodOutcome] = []
        self._recoveries = 0
        self._recovery_state = "not-needed"
        self._cap_needs: dict[str, dict] = {}
        self._suppressed_caps: set[str] = set()
        self._locators: list[str] = []
        self._source_derived = False
        self._format_violations: list = []
        self._research_used = 0
        self._research_cache: dict[str, str] = {}
        self._help_request: Optional[dict] = None
        self._knowledge_briefs = ""
        self._session_context: Optional[str] = None
        self._fix_notes: dict[str, str] = {}
        self._tool_repairs = 0
        self._presubmit_fixes_left = max(0, config.PRESUBMIT_FORMAT_FIXES)
        self._revision_fixups_left = max(0, config.REVISION_FORMAT_FIXUPS)
        self._gate_fixups_left = max(0, config.REVIEW_GATE_FIXUPS)

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #
    async def run(self, objective: str, history: Optional[str] = None,
                  seed_run: Optional[str] = None,
                  session_context: Optional[str] = None) -> str:
        self._session_context = session_context
        with self.tracer.span("plan", "planner"):
            plan = await self.planner.plan(objective, history=history,
                                           session_context=session_context)
        self.blackboard.put("objective", objective)
        # Session revision: the planner judged the new input to be FEEDBACK on
        # this session's previous deliverable — auditable state, and the run's
        # criteria now carry both the original demands and the feedback.
        if getattr(plan, "revision", None):
            self.blackboard.put("plan.revision", plan.revision)
            self.tracer.event("plan.revision", "planner", status="info",
                              of_run=plan.revision.get("of_run", ""),
                              feedback=plan.revision.get("feedback", "")[:300],
                              preserve=plan.revision.get("preserve", "")[:300])

        # Shared per-run workspace so file-producing tools can hand files to
        # file-consuming tools across agents.
        workspace = Path(config.RUNS_DIR) / self.session / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        # Risk-scaled review (#13): a user-REFERENCED source is evidence whether
        # it is local (attached file) or remote (a locator named in the
        # objective) — a run deriving factual claims from either must be
        # grounding-reviewed. A one-stage summary of a linked source was
        # observed shipping as 'grounded' with nothing ever checking its claims.
        has_user_evidence = (workspace / "input").is_dir() and \
            any((workspace / "input").iterdir())
        self._locators = evidence_mod.designated_locators(objective)
        # Persisted so a FUTURE run seeding from this one can translate this
        # run's evidence tiers (they are computed against THESE locators).
        try:
            (Path(config.RUNS_DIR) / self.session / "locators.json").write_text(
                json.dumps(self._locators))
        except OSError:
            pass
        has_external_evidence = bool(self._locators)
        # Source-derived runs (designated sources exist: attached files or
        # remote locators) are held to the retained-evidence gate; depth-only
        # risk is not — a pure synthesis chain has no payloads to retain.
        self._source_derived = has_user_evidence or has_external_evidence
        # Research state resets BEFORE knowledge resolution: the knowledge pass
        # spends the same per-run budget, and its briefs must survive into
        # stage execution (resetting later would silently wipe them).
        self._research_used = 0
        self._research_cache = {}
        self._knowledge_briefs = ""
        plan = await self._correct_proxy_first_plan(plan, objective, history)
        plan = await self._resolve_knowledge_needs(plan, objective, history)
        planned, appended = workflow.append_review_if_needed(
            plan.stages, has_user_evidence=has_user_evidence,
            has_external_evidence=has_external_evidence)
        if appended:
            self.tracer.event("review.appended", "plan", status="info",
                              reason=("user evidence transformed" if has_user_evidence
                                      else "external source referenced" if has_external_evidence
                                      else "deep chain"))
        stages = workflow.topo_order(planned)
        roster = [_stage_dict(s) for s in stages]
        self.blackboard.put("plan", roster)
        self.tracer.event("plan.created", "plan", status="info", stages=roster)

        # Cross-run continuity: make the most relevant prior run's files
        # available to this run's file tools.
        if seed_run:
            seeded = _seed_prior_workspace(config.RUNS_DIR, self.session, seed_run)
            if seeded:
                self.tracer.event("history.seeded", seed_run, status="info",
                                  files=seeded[:20], count=len(seeded))
                inherited = evidence_mod.inherit_records(
                    Path(config.RUNS_DIR) / self.session,
                    Path(config.RUNS_DIR) / seed_run, seed_run)
                if inherited:
                    self.tracer.event("evidence.inherited", seed_run, status="info",
                                      records=inherited)
            # A REVISION run's designated source includes the artifact under
            # revision itself. Deliverables are agent text — never retained by
            # a tool, so never in any workspace or ledger — which left
            # preserve-discipline criteria ("unchanged except…") structurally
            # unverifiable: the reviewer rightly refused to diff against
            # narration and the run could not be approved. Seed the prior
            # deliverable as a RETAINED, target-tier evidence file.
            #
            # DETERMINISTIC FALLBACK: the planner's `revision` judgment is a
            # single unchecked LLM emission with no second chance — when it
            # silently fails, the seeding never triggers even though the plan
            # itself NAMES the prior-run path it expects the deliverable at
            # (observed: a stage goal referenced the exact prior/<id>/ path,
            # the run discovered mid-execution that nothing was seeded there,
            # and a healthy revision blocked). A prior/ path reference in the
            # plan or objective IS the revision signal, judged by the engine.
            prior_referenced = "prior/" in (
                objective + " " + " ".join(f"{s.goal} {s.role}" for s in stages))
            if prior_referenced and not getattr(plan, "revision", None):
                self.tracer.event(
                    "revision.fallback_detected", seed_run, status="info",
                    note="the plan references prior-run paths but the planner "
                         "emitted no revision judgment — seeding the prior "
                         "deliverable deterministically")
            if getattr(plan, "revision", None) or prior_referenced:
                prior_out = Path(config.RUNS_DIR) / seed_run / "outputs"
                for name in ("DELIVERABLE.md", "ANSWER.md"):
                    src = prior_out / name
                    if not src.is_file():
                        continue
                    rel = f"prior/{seed_run}/outputs/{name}"
                    dst = workspace / rel
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_bytes(src.read_bytes())
                    except OSError:
                        continue
                    evidence_mod.record_designated(
                        Path(config.RUNS_DIR) / self.session,
                        "prior_deliverable", [rel], workspace=workspace)
                    self.tracer.event("revision.deliverable_seeded", seed_run,
                                      status="info", file=rel)
                    break

        self._sessions = 0
        self._stages_by_id = {s.id: s for s in stages}
        self._stage_notes: dict[str, dict[str, str]] = {}
        self._redo: dict[str, Callable[[str], Awaitable[str]]] = {}
        self._stage_caps: dict[str, dict] = {}  # stage id -> caps it used (for quarantine)
        outputs: dict[str, str] = {}
        # Team-channel context: completed outputs + what a follow-up needs.
        self._outputs = outputs
        self._run_ctx = (objective, history, workspace)
        self._questions_answered = 0
        self._sup_redone: set[str] = set()
        self._criteria = list(getattr(plan, "success_criteria", []) or [])
        if self._criteria:
            self.tracer.event("plan.criteria", "plan", status="info", criteria=self._criteria)
        # The initial method choice is auditable state (P0-F): persist the
        # planner's strategy rationale and the per-capability authoring
        # contracts it compiled (instance-neutral; the concrete task values
        # ride separately as verification inputs).
        self._cap_needs = {str(k): dict(v) for k, v in
                           (getattr(plan, "capability_needs", {}) or {}).items()}
        if getattr(plan, "strategy_rationale", ""):
            self.blackboard.put("plan.rationale", plan.strategy_rationale)
        # Objective-success classification signals (#17): a run is high-risk when
        # it derives claims from user-referenced evidence (local or remote) or
        # runs a very deep chain — such a run must be grounding-reviewed before
        # it can read "succeeded".
        self._high_risk = has_user_evidence or has_external_evidence or \
            workflow._transformation_depth(stages) >= config.REVIEW_DEPTH_THRESHOLD
        self._review_ran = False
        self._review_approved = False
        self._review_verdicts: list[str] = []   # authoritative, in round order
        self._blocked = False
        self._repair_stalled = False    # unchanged defect signature -> detector suspect
        self._verbatim_caps: dict[str, bool] = {}  # cap -> every observed write was verbatim
        self._replans = 0               # bounded loop re-entries on evidence gaps
        self._gaps_seen = 0             # gaps already routed to a replan
        self._failures = []             # MethodOutcome per failed stage (controller state)
        self._recoveries = 0            # bounded failure-driven strategy re-entries
        self._recovery_state = "not-needed"
        self._suppressed_caps = set()   # methods that failed this run; need new rationale
        self._acq_failures = []         # run-scoped, cross-contract failure memory
        self._format_violations = []    # engine-measured count-criteria failures
        self._help_request = None       # set when the brain decides to stop and ask the user
        self._fix_notes = {}            # stage id -> accumulated fix-round lessons
        self._tool_repairs = 0          # agent-reported tool-defect repairs spent
        self._presubmit_fixes_left = max(0, config.PRESUBMIT_FORMAT_FIXES)

        last_done: Optional[str] = None
        failure_note: Optional[str] = None
        i = -1
        while i + 1 < len(stages):   # index-based: the plan may EXTEND mid-run
            i += 1
            stage = stages[i]
            try:
                with self.tracer.span("stage", stage.id, pattern=stage.pattern) as sp:
                    out = await self._run_stage(stage, stages, outputs, objective, history, workspace)
                    # Surface the stage's result in the live view: mid-run output
                    # meant for the user (a how-to, a warning) must not be
                    # invisible until the run ends.
                    sp.set(output_head=(out or "")[:700])
            except Exception as e:  # noqa: BLE001
                # A stage failure — including capability acquisition dying BEFORE
                # any agent existed — is an OBSERVATION about the method, not (yet)
                # a verdict on the run: classify it, preserve what was learned,
                # and re-enter strategy adjudication for the residual requirement.
                # Observed failure this closes: authoring exhausted its retries,
                # the exception escaped, and no alternative strategy was ever
                # considered because the only feedback path required a running
                # agent to report it.
                outcome = self._method_outcome(stage, e, workspace)
                self._failures.append(outcome)
                prior_failures = self.blackboard.get("method.failures", []) or []
                self.blackboard.put("method.failures",
                                    prior_failures + [dataclasses.asdict(outcome)])
                self.tracer.event("stage.failed", stage.id, status="failed",
                                  error=f"{type(e).__name__}: {str(e)[:300]}",
                                  phase=outcome.phase,
                                  failure_class=outcome.failure_class,
                                  attempts=outcome.attempts)
                if await self._recover_failed_stage(stage, stages, i, objective,
                                                    outputs, outcome):
                    continue
                if last_done is None:
                    break   # nothing to deliver — finalized below as controller state
                # Best-effort delivery: a completed draft must never be vaporized
                # by a later stage's death.
                failure_note = (f"[engine note] stage '{stage.id}' failed "
                                f"({type(e).__name__}: {str(e)[:200]}); delivering the last "
                                f"completed stage's result ('{last_done}') as-is.")
                break
            outputs[stage.id] = out
            self.blackboard.put(f"output:{stage.id}", out)
            last_done = stage.id
            # Stable aggregate output per stage (#24): map digests and review
            # verdicts otherwise only exist as per-worker/per-round files, and
            # the live view's "full output: outputs/<stage>.md" pointer dangles.
            agg = Path(config.RUNS_DIR) / self.session / "outputs" / f"{stage.id}.md"
            if not agg.exists():
                agg.parent.mkdir(parents=True, exist_ok=True)
                agg.write_text(f"# {stage.id} [{stage.pattern}] — aggregate output\n\n{out or ''}")

            # Mechanical artifact repair, IN the loop so a later review stage
            # judges the REPAIRED artifact and nothing rewrites reviewed work.
            # Repairs edit against the ACTUAL file content with minimal-change
            # instructions — a blind regeneration was observed to regress a
            # reviewed artifact back to hallucinated content.
            if stage.id in self._redo:
                out = await self._repair_artifacts(stage.id, outputs, workspace, out)
                if outputs[stage.id] != out:
                    outputs[stage.id] = out
                    self.blackboard.put(f"output:{stage.id}", out)
                last_done = stage.id

            # Executive check: does what just happened still serve the objective?
            # Single-stage runs are NOT exempt — one stage was exactly where an
            # unassessed answer shipped with two criteria visibly violated.
            if self.supervisor is not None:
                d = await self.supervisor.consult(
                    "stage_completed",
                    self._digest(just_completed=(stage.id, out)),
                    allowed=("proceed", "inform_user", "redo_stage"))
                if d.action == "inform_user" and self.broker is not None:
                    self.broker.inform(d.message)
                elif d.action == "redo_stage":
                    redo = self._redo.get(stage.id)
                    if redo is None or stage.id in self._sup_redone:
                        # A dropped executive directive must be a visible fact,
                        # not a silent no-op: four runs shipped unverified with
                        # the supervisor's redo order recorded and nothing in
                        # the trace saying it could not be honored.
                        self.tracer.event(
                            "supervisor.redo_unavailable", stage.id, status="failed",
                            reason=d.reason[:300],
                            note=("stage was already redone once this run"
                                  if stage.id in self._sup_redone else
                                  f"no redo is registered for this stage "
                                  f"(pattern '{stage.pattern}')"))
                    else:
                        self._sup_redone.add(stage.id)
                        self.tracer.event("supervisor.redo", stage.id, status="info",
                                          guidance=d.guidance[:300], reason=d.reason)
                        out = await redo(d.guidance, out)
                        outputs[stage.id] = out
                        self.blackboard.put(f"output:{stage.id}", out)

            # Feedback transition: an agent reported that the objective depends
            # on something its tools could not OBSERVE. Re-enter the core loop
            # for the residual need — plan additional observation, acquire the
            # tools for it, keep executing. Bounded; never a task-shaped branch.
            await self._maybe_extend(stages, i, objective, outputs)

        if last_done is not None:
            answer = outputs.get(last_done, "")
            # The PURE deliverable — before any engine banner/note/appendix — gets
            # its own channel, and it is the fenced PAYLOAD when the stage emitted
            # one: "exactly one paragraph" must have a file that IS exactly one
            # paragraph, not preamble + paragraph + change log (observed V060).
            # It comes from the artifact-of-record's OWNING stage — after
            # extensions, the last completed deliverable-owning stage — never
            # implicitly from whatever stage ran last (an inspection report
            # was observed shipping as DELIVERABLE.md while the requested
            # artifact sat in the workspace).
            deliv_ids = workflow.deliverable_stage_ids(stages)
            src = next((s.id for s in reversed(stages)
                        if s.id in deliv_ids and s.id in outputs), last_done)
            if src != last_done:
                self.tracer.event("deliverable.source", src, status="info",
                                  note=f"payload from '{src}', not terminal '{last_done}'")
            payload = (workflow.extract_deliverable(outputs.get(src, ""))
                       or workflow.extract_deliverable(answer)
                       or outputs.get(src, "") or answer)
            deliverable_dir = Path(config.RUNS_DIR) / self.session / "outputs"
            deliverable_dir.mkdir(parents=True, exist_ok=True)
            (deliverable_dir / "DELIVERABLE.md").write_text(payload or "")
            # Countable format criteria are MEASURED against the deliverable of
            # record (artifact when one exists, else the payload) — a measured
            # violation is authoritative over any verdict prose. (Slide/page
            # counts defer to the rendered artifact when one exists.)
            checks, meas_target = self._measured_checks(payload, workspace)
            self._format_violations = criteria_mod.violations(checks)
            if checks:
                self.tracer.event(
                    "criteria.measured", "run", status="info", target=meas_target,
                    checks=[f"{c.op} {c.bound} {c.unit}: measured {c.measured} "
                            f"({'ok' if c.ok else 'VIOLATED' if c.gating else 'advisory'})"
                            for c in checks])
            if failure_note:
                answer = (answer + "\n\n" + failure_note).strip()
        else:
            # Nothing completed: no DELIVERABLE.md exists (a deliverable that is
            # a failure notice would be a lie), and the user's answer is a
            # controller-level diagnosis — what was tried, what remains open,
            # why execution stopped — never a raw traceback.
            answer = self._failure_report(workspace)

        # Persistent artifact defects at delivery: quarantine ONLY the tool the
        # defect is causally attributable to. Attribution requires all three:
        # the defective file lives in that capability's own output dir; the
        # capability was NOT observed writing supplied content verbatim (a
        # conduit cannot have introduced a content defect — blame belongs to
        # the content's generator); and repair did not stall on an unchanged
        # signature (an immovable finding indicts the DETECTOR, not the tool).
        lingering = artifact_issues(workspace, definite_only=True)
        if lingering and self.registry is not None:
            if self._repair_stalled:
                self.tracer.event("quarantine.skipped", "run", status="info",
                                  reason="defect signature survived repair unchanged — "
                                         "detector finding is suspect, no tool is blamed",
                                  issues=lingering[:4])
            else:
                producers = {Path(d.split(" [", 1)[0]).parts[0]
                             for d in lingering if "/" in d.split(" [", 1)[0]}
                known = {n for caps in self._stage_caps.values() for n in caps}
                for name in sorted(producers & known):
                    if self._verbatim_caps.get(name):
                        self.tracer.event("capability.exonerated", name, status="info",
                                          reason="every observed write matched the supplied "
                                                 "content byte-for-byte — the defect came "
                                                 "from the content's generator, not this tool")
                        continue
                    self.registry.quarantine(
                        name, "repeatedly produced defective artifact output")
                    self.tracer.event("capability.quarantined", name, status="failed",
                                      reason="artifact defects persisted after repair")

        # Objective-success classification (#17/#20): operational completion is
        # NOT objective success, and the reason names the failing DIMENSION.
        self.classification, reason = self._classify(bool(failure_note), workspace, last_done)
        self.tracer.event("run.classification", "run", status="info",
                          classification=self.classification, reason=reason)
        if self.classification != "succeeded":
            answer = (f"[result: {self.classification.upper()}] {reason}\n\n" + answer).strip()
        # A brain-decided stop-and-ask leads with its structured help request:
        # how far it got, what it tried, why it stopped, and what it needs.
        if self._help_request:
            answer = (self._help_request_block() + "\n\n" + answer).strip()

        # The deliverable is the FILES, not the producing agent's self-account:
        # list them with observed facts so the answer cannot overclaim.
        produced = [(p.relative_to(workspace), p.stat().st_size)
                    for p in sorted(workspace.rglob("*"))
                    if p.is_file() and p.relative_to(workspace).parts[0] not in ("prior", "input")]
        if produced:
            answer += ("\n\n---\n[engine audit — NOT part of the deliverable] "
                       "FILES actually produced this run:\n"
                       + "\n".join(f"- workspace/{rel} ({size:,} bytes)" for rel, size in produced))

        self.blackboard.put("answer", answer)
        answer_dir = Path(config.RUNS_DIR) / self.session / "outputs"
        answer_dir.mkdir(parents=True, exist_ok=True)
        (answer_dir / "ANSWER.md").write_text(answer or "")
        return answer or "(no answer produced)"

    async def _correct_proxy_first_plan(self, plan, objective: str,
                                        history: Optional[str]):
        """Proactive tool creation (user directive: 'if the tool required is not
        available, create it — do not treat a less relevant alternative as
        sufficient'). A source-derived objective whose plan skipped direct
        observation with NO legitimate intrinsic blocker is a proxy-first plan:
        the planner fell back to a weaker channel because the direct tool was
        not already installed. Tool-absence is never a blocker — the system
        authors tools — so re-plan ONCE with a pointed corrective. Bounded and
        fail-open: any error, or a planner that still cannot observe directly,
        keeps the best plan we have."""
        from .planner import observation_is_accountable

        if not self._source_derived or observation_is_accountable(
                getattr(plan, "direct_observation", {})):
            return plan
        direct = getattr(plan, "direct_observation", {}) or {}
        self.tracer.event("plan.proxy_first", "planner", status="failed",
                          decision=direct.get("decision", "unstated"),
                          blocker=direct.get("blocker_class", ""))
        corrective = (
            "REVISION REQUIRED — your plan does not directly observe the designated "
            "source, and you recorded no legitimate blocker. The ABSENCE OF AN EXISTING "
            "TOOL IS NOT A BLOCKER: this system AUTHORS tools at runtime. Redesign so the "
            "plan OBSERVES THE SOURCE ITSELF — decompose it into atomic capabilities you "
            "NAME for the system to build (acquire the object; convert its carrying signal "
            "into files) followed by an ordinary agent stage that PERCEIVES those files "
            "(reading text, looking at images — the agent is natively multimodal, so do NOT "
            "author a vision/OCR/caption model). Do not substitute web search or other "
            "testimony for direct observation. Only if direct observation is genuinely "
            "infeasible, set direct_observation.decision='infeasible' with a real "
            "blocker_class (requires_credential / resource_ceiling / no_free_means / "
            "physical_access / user_authority).")
        try:
            revised = await self.planner.plan(objective, history=history,
                                              corrective=corrective,
                                              session_context=self._session_context)
        except Exception as e:  # noqa: BLE001 — correction must never break planning
            self.tracer.event("plan.corrected", "planner", status="failed",
                              error=f"{type(e).__name__}: {str(e)[:160]}")
            return plan
        self.tracer.event(
            "plan.corrected", "planner", status="info",
            accountable=observation_is_accountable(getattr(revised, "direct_observation", {})),
            stages=[s.id for s in revised.stages])
        return revised if revised.stages else plan

    def _write_corpus_manifest(self, workspace: Path, entries) -> Optional[str]:
        """Materialize the corpus enumeration as a CITABLE evidence file.

        A negative claim scoped to a bounded corpus ('no X exists in the
        sources') is groundable only by observation of the WHOLE corpus — and
        that observation existed nowhere as a file, so a sound absence claim
        citing 'the ledger lists exactly N files' was deterministically voided
        as a miscitation and the run shipped unverified. The manifest is
        deterministic engine output, recorded as consuming every
        target-lineage file (lineage classifies it 'derived'); the claims
        gate itself is unchanged. Returns the workspace-relative path, or
        None when there is no target-lineage evidence to enumerate."""
        lineage = [e for e in evidence_mod.target_lineage(entries)
                   if not str(e.file).startswith("corpus_manifest/")]
        if not lineage:
            return None
        rel = "corpus_manifest/manifest.md"
        p = Path(workspace) / rel
        lines = [
            "# Corpus manifest — complete enumeration of retained target-lineage evidence",
            "",
            f"Engine-generated, deterministic. This run's designated-source corpus "
            f"comprises exactly {len(lineage)} retained file(s); no other payload "
            "from the designated sources exists in this run. An absence claim "
            "scoped to this corpus may cite THIS file as its negative evidence.",
            "",
        ]
        for e in lineage:
            lines.append(f"- {e.file} (tier={e.tier}; via {e.capability}"
                         + (f"; sha256={e.sha256}" if e.sha256 else "") + ")")
        content = "\n".join(lines) + "\n"
        try:
            if p.is_file() and p.read_text() == content:
                return rel      # fresh already — do not re-record provenance
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        except OSError:
            return None
        evidence_mod.record(Path(config.RUNS_DIR) / self.session, "corpus_manifest",
                            [rel], [], "full",
                            inputs=[e.file for e in lineage][:24],
                            state="observed_present", role="observation",
                            workspace=Path(workspace))
        self.tracer.event("evidence.manifest", "corpus_manifest", status="info",
                          enumerated=len(lineage))
        return rel

    def _perceivable_artifacts(self, workspace: Path) -> tuple[list[str], list[str]]:
        """Files an audience will LOOK at: (renderable, images). Classified by
        the producing tool's DECLARED role in the provenance log — only
        'artifact'-role output qualifies. Extension alone must not decide:
        retained evidence (a fetched page.html, an observed screenshot) is
        input to judgment, not the thing being judged. Renderable formats need
        the render capability first; images are directly perceivable."""
        from .capabilities.runtime import _IMAGE_EXTS

        root = Path(workspace)
        renderable: list[str] = []
        images: list[str] = []
        if not root.is_dir():
            return renderable, images
        seen: set[str] = set()
        for rec in evidence_mod._records(root.parent):
            if rec.get("role") != "artifact" or rec.get("cap") == _RENDER_NEED_KEY:
                continue
            for f in rec.get("files") or []:
                rel = str(f)
                if rel in seen or not (root / rel).is_file():
                    continue
                seen.add(rel)
                ext = Path(rel).suffix.lower()
                if ext in _RENDERABLE_EXTS:
                    renderable.append(rel)
                elif ext in _IMAGE_EXTS:
                    images.append(rel)
        return renderable[:6], images[:12]

    @staticmethod
    def _is_render_record(rec: dict) -> bool:
        """A render, identified by SHAPE not name (tools are contracts): the
        call consumed a renderable-format file and produced images. Covers the
        seeded render capability AND any equivalent tool a plan authored."""
        from .capabilities.runtime import _IMAGE_EXTS

        files = rec.get("files") or []
        if not files or rec.get("cap") == "input":
            return False
        if not all(Path(str(f)).suffix.lower() in _IMAGE_EXTS for f in files):
            return False
        return any(Path(str(i)).suffix.lower() in _RENDERABLE_EXTS
                   for i in rec.get("inputs") or [])

    def _last_render(self, workspace: Path) -> tuple[Optional[int], Optional[dict]]:
        records = evidence_mod._records(Path(workspace).parent)
        idx, last = None, None
        for i, rec in enumerate(records):
            if self._is_render_record(rec):
                idx, last = i, rec
        return idx, last

    def _rendered_page_count(self, workspace: Path) -> Optional[int]:
        """Pages of the artifact's MOST RECENT render (from the provenance log,
        so stale images from an earlier render round can't skew the count).
        None when nothing has been rendered."""
        from .capabilities.runtime import _IMAGE_EXTS

        _, last = self._last_render(workspace)
        if last is None:
            return None
        return sum(1 for f in last["files"]
                   if Path(str(f)).suffix.lower() in _IMAGE_EXTS)

    def _render_is_stale(self, workspace: Path) -> bool:
        """True when the artifact was REGENERATED after its last render: the
        page images depict a superseded draft, so neither a reviewer's eyes
        nor the page-count gate may trust them (observed: round 2 critiqued
        already-fixed defects from stale images, and the terminal page count
        certified a deck that no longer existed)."""
        records = evidence_mod._records(Path(workspace).parent)
        idx, last = self._last_render(workspace)
        if last is None:
            return False
        inputs = {str(i) for i in last.get("inputs") or []}
        if not inputs:
            return False    # unattributable render — cannot adjudicate staleness
        return any(str(f) in inputs
                   for rec in records[idx + 1:]
                   for f in rec.get("files") or [])

    _MEASURABLE_ARTIFACT_EXTS = (".md", ".markdown", ".txt", ".html", ".htm")

    def _measurement_target(self, payload: str, workspace: Path) -> tuple[str, str]:
        """(text, label): the object count criteria are measured against.

        THE MEASUREMENT-TARGET CONTRACT: when the run produced a deliverable
        ARTIFACT file in a text-measurable format, the artifact is the
        deliverable of record and structural counts follow IT — the payload
        may faithfully summarize or point at it. Otherwise the payload is the
        deliverable and is measured directly. Observed both ways: a compliant
        HTML dashboard measured '0 of 4 sections' because its one-line pointer
        payload was counted, and grading swung 10/11 -> 6/11 on unchanged
        analysis quality — pure measurement-target variance. Same filter as
        _perceivable_artifacts: DECLARED artifact role only, renders excluded.

        Among candidates, the payload's OWN REFERENCE designates the
        deliverable of record: an author naming the produced file it ships
        outranks recency (observed: template-probe invocations after the real
        artifact made 'last-produced' measure a placeholder file, in a
        revision remeasure and again at run end — and collision-deduped
        names guarantee such siblings accumulate). Recency remains the
        tie-breaker among referenced files and the rule when the payload
        references none (a regeneration supersedes)."""
        root = Path(workspace)
        candidates: list[str] = []
        if root.is_dir():
            for rec in evidence_mod._records(root.parent):
                if rec.get("role") != "artifact" or rec.get("cap") == _RENDER_NEED_KEY:
                    continue
                for f in rec.get("files") or []:
                    rel = str(f)
                    if (Path(rel).suffix.lower() in self._MEASURABLE_ARTIFACT_EXTS
                            and (root / rel).is_file()):
                        candidates.append(rel)
        referenced = [rel for rel in candidates
                      if rel in payload or Path(rel).name in payload]
        chosen = (referenced or candidates)[-1] if candidates else ""
        if not chosen:
            return payload, "the deliverable PAYLOAD"
        try:
            text = (root / chosen).read_text(errors="replace")
        except OSError:
            return payload, "the deliverable PAYLOAD"
        if chosen.lower().endswith((".html", ".htm")):
            text = criteria_mod.html_countable_text(text)
        return text, f"the produced ARTIFACT {chosen}"

    def _measured_checks(self, payload: str, workspace: Path) -> tuple[list, str]:
        """(checks, target label) — all engine-measured count checks, measured
        against the deliverable OF RECORD (see _measurement_target). When the
        artifact has been RENDERED, the render is authoritative for slide/page
        counts — the text heuristic (--- separators) only stands when nothing
        was rendered. Both directions matter: a stray separator must not void
        a correct artifact, and clean prose must not excuse an 11-page
        artifact against an explicit 10."""
        target_text, target = self._measurement_target(payload, workspace)
        checks = criteria_mod.check_counts(self._criteria, target_text)
        pages = self._rendered_page_count(workspace)
        if pages is not None:
            checks = [c for c in checks if c.unit not in ("slide", "page")]
            # A STALE render measures a superseded draft — no measurement at
            # all beats certifying the wrong artifact; the approval gate
            # separately demands a re-render.
            if not self._render_is_stale(workspace):
                checks += criteria_mod.artifact_page_check(self._criteria, pages)
        return checks, target

    async def _resolve_knowledge_needs(self, plan, objective: str,
                                       history: Optional[str]):
        """Define GOOD before building: the plan declared knowledge gaps —
        domain standards/conventions the deliverable's quality depends on that
        the objective does not spell out. Resolve them through the research
        channel (shared budget), then re-invoke the planner ONCE with the
        briefs so the final success criteria ENCODE the researched standards
        as checkable conditions. One pass, fail-open: mediocrity was never a
        trigger before — a run could be grounded, criteria-clean, and amateur,
        because 'what does good look like here?' was never asked."""
        needs = list(getattr(plan, "knowledge_needs", []) or [])
        if not needs:
            return plan
        briefs: list[str] = []
        for n in needs[:3]:
            topic, why = n.get("topic", ""), n.get("why", "")
            self.tracer.event("knowledge.researching", "planner", status="info",
                              topic=topic[:200], why=why[:200])
            brief = await self._research(topic, why=why or "informs the plan's "
                                         "quality criteria for the deliverable")
            briefs.append(f"### {topic}\n{brief}")
        self._knowledge_briefs = "\n\n".join(briefs)[:12000]
        corrective = (
            "KNOWLEDGE BRIEFS — the standards you asked to research (method knowledge; "
            "not evidence about the task's designated sources):\n"
            + self._knowledge_briefs
            + "\n\nRe-emit the FULL plan now, informed by these briefs: encode the "
              "researched standards as CHECKABLE success_criteria (alongside the "
              "objective's own demands), adjust stage goals where the briefs change the "
              "approach, and do NOT redeclare knowledge_needs — this is the single "
              "knowledge-informed pass.")
        try:
            revised = await self.planner.plan(objective, history=history,
                                              corrective=corrective,
                                              session_context=self._session_context)
        except Exception as e:  # noqa: BLE001 — knowledge must never break planning
            self.tracer.event("knowledge.replan_failed", "planner", status="failed",
                              error=f"{type(e).__name__}: {str(e)[:160]}")
            return plan
        self.tracer.event("knowledge.informed", "planner", status="info",
                          topics=[n.get("topic", "")[:80] for n in needs[:3]],
                          criteria_after=list(getattr(revised, "success_criteria", []))[:12])
        return revised if revised.stages else plan

    def _workspace_server(self, workspace: Path):
        """A general, always-available file viewer for stage agents: open any
        file the run retained or produced (workspace-relative). IMAGES are
        returned AS IMAGES — the reasoning model is multimodal, so 'describe
        these frames/screenshots/scans' is the agent's OWN ability and needs no
        vision-model tool. Text returns an excerpt. This is the primitive that
        makes a plan of [author a frame/segment/page extractor] -> [an agent
        perceives the produced files] executable across stages."""
        from claude_agent_sdk import create_sdk_mcp_server, tool

        from .capabilities.runtime import image_blocks

        @tool("read_file",
              "Open a file the run holds (workspace-relative path — e.g. a path from a "
              "tool's produced-files list or an upstream note). IMAGES (png/jpg/webp/…) "
              "come back as images for you to LOOK AT directly; text files return an "
              "excerpt. Use it to PERCEIVE retained evidence — frames, screenshots, "
              "scans, rendered pages, charts — rather than guessing their content.",
              {"type": "object", "properties": {"path": {"type": "string"}},
               "required": ["path"]})
        async def read_file(args):
            return self._read_workspace(workspace, str(args.get("path", "")))

        @tool("list_files",
              "Enumerate the files the run holds (workspace-relative paths with sizes), "
              "optionally filtered by a glob pattern (matched against the full path AND "
              "the bare filename — e.g. '*.png', 'prior/*', '*frame_01*'). Returns the "
              "total count and pages with an explicit offset when long. Use it to KNOW "
              "what exists — never guess paths, and never conclude a file is absent "
              "from a truncated listing or an error message.",
              {"type": "object", "properties": {"pattern": {"type": "string"},
                                                "offset": {"type": "integer"}}})
        async def list_files(args):
            return self._list_workspace(workspace, str(args.get("pattern", "") or ""),
                                        int(args.get("offset", 0) or 0))

        return create_sdk_mcp_server(name="workspace", version="1.0.0",
                                     tools=[read_file, list_files])

    def _read_workspace(self, workspace: Path, rel: str) -> dict:
        """Image-aware workspace file read (the read_file tool body, split out so
        it is directly testable). Images -> image content block; text -> excerpt;
        path traversal and missing files -> honest error. The whole body is
        guarded: an engine bug here must fail LOUDLY as a clearly-labeled
        internal error the agent will not grind on, never a silent retry sink
        (a `kind=` kwarg collision with Tracer.event made every image read
        return an opaque error and 12 workers retried it into the session cap)."""
        from .capabilities.runtime import image_blocks

        try:
            root = Path(workspace).resolve()
            rel = str(rel or "").strip().lstrip("/")
            p = (root / rel).resolve()
            if not str(p).startswith(str(root)) or not p.is_file():
                return {"content": [{"type": "text",
                                     "text": _missing_file_error(root, rel)}],
                        "is_error": True}
            from .capabilities.runtime import _IMAGE_EXTS

            is_image = p.suffix.lower() in _IMAGE_EXTS
            imgs = image_blocks([str(p.relative_to(root))], root, 1,
                                config.OBSERVATION_MAX_IMAGE_BYTES,
                                budget_bytes=config.transport_result_budget() - 4096)
            self.tracer.event("agent.read_file", "workspace", status="info",
                              path=rel[:160], file_kind="image" if imgs else "text")
            if imgs:
                return {"content": [{"type": "text",
                                     "text": f"[{rel} — shown as image]"}] + imgs}
            if is_image:
                # Never fall through to a text read of image bytes; say why the
                # image cannot be shown instead of returning garbage.
                return {"content": [{"type": "text", "text": (
                        f"[{rel}] is an image ({p.stat().st_size:,} bytes) that could not "
                        "be displayed within the transport-safe size budget. It exists and "
                        "downstream tools can consume it by path.")}]}
            text = p.read_text(errors="ignore")
        except Exception as e:  # noqa: BLE001 — an engine bug must not become a retry loop
            self.tracer.event("agent.read_file.error", "workspace", status="failed",
                              path=str(rel)[:160], error=f"{type(e).__name__}: {str(e)[:200]}")
            return {"content": [{"type": "text", "text": (
                    f"INTERNAL ERROR reading {rel!r} ({type(e).__name__}). This is an "
                    "engine fault, not a bad path — do NOT retry this file or others; "
                    "report a blocker so the run can recover.")}], "is_error": True}
        more = f"\n[…{len(text) - 8000:,} more chars]" if len(text) > 8000 else ""
        return {"content": [{"type": "text", "text": f"[{rel}]\n{text[:8000]}{more}"}]}

    def _list_workspace(self, workspace: Path, pattern: str = "",
                        offset: int = 0, page_size: int = 200) -> dict:
        """Deterministic, bounded workspace enumeration (the list_files tool
        body, split out so it is directly testable). Paging is explicit and
        truncation is labeled as truncation — a bounded listing silently read
        as 'everything there is' was observed grounding a false absence
        conclusion that cost a run its approval."""
        import fnmatch

        try:
            root = Path(workspace).resolve()
            files = _workspace_files(root)
            if pattern:
                files = [f for f in files
                         if fnmatch.fnmatch(f, pattern)
                         or fnmatch.fnmatch(f.rpartition("/")[2], pattern)]
            total = len(files)
            self.tracer.event("agent.list_files", "workspace", status="info",
                              pattern=pattern[:120], total=total, offset=offset)
            if total == 0:
                return {"content": [{"type": "text", "text":
                        (f"no files match pattern {pattern!r}" if pattern
                         else "the workspace holds no files yet")}]}
            offset = max(0, min(int(offset), total - 1))
            page = files[offset:offset + page_size]
            lines = []
            for f in page:
                try:
                    lines.append(f"{f}\t{(root / f).stat().st_size:,} bytes")
                except OSError:
                    lines.append(f)
            head = (f"{total} file(s)" + (f" matching {pattern!r}" if pattern else "")
                    + (f"; showing {offset + 1}–{offset + len(page)}"
                       if total > len(page) else ""))
            tail = ""
            if offset + len(page) < total:
                tail = ("\nTRUNCATED at this page — files beyond it EXIST; absence "
                        "from this page is not absence from the workspace. Next page: "
                        f"list_files(offset={offset + len(page)}"
                        + (f", pattern={pattern!r})" if pattern else ")"))
            return {"content": [{"type": "text",
                                 "text": head + ":\n" + "\n".join(lines) + tail}]}
        except Exception as e:  # noqa: BLE001 — an engine bug must not become a retry loop
            self.tracer.event("agent.list_files.error", "workspace", status="failed",
                              error=f"{type(e).__name__}: {str(e)[:200]}")
            return {"content": [{"type": "text", "text": (
                    f"INTERNAL ERROR listing files ({type(e).__name__}). This is an "
                    "engine fault, not a bad pattern — do NOT retry; report a blocker "
                    "so the run can recover.")}], "is_error": True}

    async def _repair_artifacts(self, stage_id: str, outputs: dict, workspace: Path,
                                out: str) -> str:
        """Bounded mechanical repair of engine-detected artifact defects,
        editing against the artifact's REAL current content (never a blind
        regeneration from notes). Two honesty guards: a file too large for the
        prompt is never presented as a silently-clipped 'CURRENT content' (the
        clip itself creates truncation), and an attempt that leaves the defect
        signature UNCHANGED stops the loop — repeating it can only burn budget,
        and an immovable finding means the DETECTOR is the suspect."""
        prev: Optional[list[str]] = None
        for attempt in range(1, config.ARTIFACT_REPAIRS + 1):
            defects = artifact_issues(workspace, definite_only=True)
            if not defects:
                break
            if defects == prev:
                self._repair_stalled = True
                self.tracer.event("artifact.repair.stalled", stage_id, status="failed",
                                  attempt=attempt, issues=defects[:6],
                                  note="defect signature unchanged after repair — "
                                       "detector finding is suspect; not retrying")
                break
            prev = defects
            # The current content of each defective file IS the base to edit.
            current: list[str] = []
            names = {d.split(" [", 1)[0] for d in defects}
            for p in sorted(Path(workspace).rglob("*")):
                if p.is_file() and str(p.relative_to(workspace)) in names:
                    try:
                        text = p.read_text(errors="ignore")
                    except OSError:
                        continue
                    if len(text) > config.REPAIR_MAX_CONTENT_CHARS:
                        # A clipped prefix labeled "CURRENT content" makes the
                        # repairer re-emit a truncated file — skip, honestly.
                        self.tracer.event("artifact.repair.skipped", stage_id,
                                          status="info", file=p.name, size=len(text),
                                          reason="file exceeds the repair prompt budget; "
                                                 "left to review instead of clipping it")
                        continue
                    current.append(f"=== {p.relative_to(workspace)} "
                                   f"(CURRENT content, complete) ===\n{text}")
            if not current:
                break
            self.tracer.event("artifact.repair", stage_id, status="info",
                              attempt=attempt, issues=defects[:6])
            out = await self._redo[stage_id](
                "MECHANICAL DEFECTS in the produced artifact. Make MINIMAL, TARGETED edits to "
                "the CURRENT content below — fix ONLY the flagged defects (complete truncated "
                "fragments with real content, remove raw markup from rendered text, give the "
                "artifact a real title); do NOT redesign, restructure, or change anything "
                "else. Re-emit the full corrected artifact.\n\nDEFECTS:\n"
                + "\n".join(f"- {d}" for d in defects)
                + "\n\n" + "\n\n".join(current), out)
        return out

    async def _maybe_extend(self, stages: list, i: int, objective: str,
                            outputs: dict) -> None:
        """Bounded mid-run re-entry of the core loop (analyze → requirements →
        acquire → execute) for UNMET OBSERVATION NEEDS agents reported via
        report_evidence_gap. The alternative method is induced from the need by
        the planner — never from a domain branch in the engine. Observed
        failure this closes: 'transcript unavailable' changed the data but not
        the strategy, and the gap was filled from remembered narration."""
        if self.blackboard is None or self._replans >= config.REPLANS_PER_RUN:
            return
        extend = getattr(self.planner, "extend", None)
        if extend is None:
            return
        gaps = (self.blackboard.get("evidence.gaps", []) or [])[self._gaps_seen:]
        if not gaps:
            return
        self._gaps_seen += len(gaps)
        self._replans += 1
        completed = "\n".join(f"[{sid}] {(out or '')[:400]}" for sid, out in outputs.items())
        try:
            result = await extend(objective, completed, gaps,
                                  [s.id for s in stages])
        except Exception as e:  # noqa: BLE001 — replanning must never kill the run
            self.tracer.event("plan.extend.failed", "planner", status="failed",
                              error=f"{type(e).__name__}: {str(e)[:200]}")
            return
        # A strategy change may invalidate criteria that encoded the FAILED
        # mechanism ("traceable to the transcript" after the transcript yielded
        # nothing) — the planner may recompile them; the user's objective and
        # explicit constraints stay authoritative.
        new_stages, new_criteria, new_needs = self._unpack_extension(result)
        if new_criteria:
            self.tracer.event("plan.criteria.recompiled", "planner", status="info",
                              before=self._criteria, after=new_criteria)
            self._criteria = list(new_criteria)
        if new_needs:
            self._cap_needs.update(new_needs)
        if not new_stages:
            self.tracer.event("plan.extended", "planner", status="info",
                              added=[], note="no feasible additional observation")
            return
        # Insert before the first still-pending review so the verifier judges
        # the run WITH the new evidence.
        insert_at = next((j for j in range(i + 1, len(stages))
                          if stages[j].pattern == REVIEW), len(stages))
        for off, s in enumerate(new_stages):
            stages.insert(insert_at + off, s)
            self._stages_by_id[s.id] = s
        self._retarget_pending_review(stages, insert_at + len(new_stages), new_stages)
        # Re-verify after new observation: when the extension lands AFTER a
        # review that did not approve (e.g. its unmet evidence demand is what
        # drove this replan), append a fresh review over the new terminal —
        # new evidence without re-verification would ship unreviewed.
        if self._review_ran and not self._review_approved and \
                not any(s.pattern == REVIEW for s in stages[i + 1:]):
            src = next((s for s in reversed(stages[:i + 1]) if s.pattern == REVIEW), None)
            if src is not None:
                rid = f"{src.id}-recheck"
                while rid in self._stages_by_id:
                    rid += "x"
                recheck = StageSpec(
                    id=rid, pattern=REVIEW, role=src.role, goal=src.goal,
                    capabilities=list(src.capabilities),
                    depends_on=[new_stages[-1].id]
                    + [d for d in src.depends_on if d != new_stages[-1].id],
                    rounds=src.rounds)
                stages.append(recheck)
                self._stages_by_id[rid] = recheck
                self.tracer.event("review.reappended", rid, status="info",
                                  draft=new_stages[-1].id, after=src.id)
        self.tracer.event("plan.extended", "planner", status="info",
                          added=[s.id for s in new_stages],
                          needs=[g.get("need", "")[:120] for g in gaps])

    @staticmethod
    def _unpack_extension(result) -> tuple[list, list, dict]:
        """Planner.extend evolved from a bare stage list -> (stages, criteria) ->
        (stages, criteria, capability_needs); accept every shape."""
        if isinstance(result, tuple):
            parts = list(result)
            stages = parts[0] if parts else []
            criteria = parts[1] if len(parts) > 1 else []
            needs = parts[2] if len(parts) > 2 else {}
            return list(stages or []), list(criteria or []), dict(needs or {})
        return list(result or []), [], {}

    def _retarget_pending_review(self, stages: list, from_idx: int, new_stages: list) -> None:
        """Graph reconciliation, not just insertion: the grafted stages' LAST
        stage is the new artifact-of-record, so the first still-pending review
        must treat IT as the draft it verifies and repairs (deps[0]); the other
        new stages join as evidence. Observed failure this closes: review kept
        repairing the superseded original stage while the extension's synthesis
        stage — the actual deliverable — sat unreviewable as evidence-only."""
        pending = next((s for s in stages[from_idx:] if s.pattern == REVIEW), None)
        if pending is None:
            return
        # The review's draft is the artifact-of-record, not blindly the last
        # grafted stage: when any stage is MARKED deliverable, the newest such
        # stage is the draft and everything else is evidence (observed: an
        # inspection stage landed last, became 'the draft', and its report
        # shipped as the deliverable while the artifact went unreviewed).
        marked = [s for s in stages if getattr(s, "deliverable", False)
                  and s.pattern != REVIEW]
        terminal = marked[-1].id if marked else new_stages[-1].id
        evidence = [d for d in pending.depends_on if d != terminal] + \
                   [s.id for s in new_stages if s.id != terminal
                    and s.id not in pending.depends_on]
        pending.depends_on = [terminal] + evidence
        self.tracer.event("review.retargeted", pending.id, status="info",
                          draft=terminal, evidence=evidence)

    def _input_files(self, workspace: Path) -> list[str]:
        """The files the USER provided to this run (workspace/input/), with
        sizes. Any judgment about a file-related failure must see this
        inventory: a persistence decision was observed asking the user to
        're-upload' a file that was sitting in the workspace all along —
        because its digest could not see input/."""
        inp = Path(workspace) / "input"
        if not inp.is_dir():
            return []
        return [f"input/{p.relative_to(inp)} ({p.stat().st_size:,} bytes)"
                for p in sorted(inp.rglob("*")) if p.is_file()][:12]

    def _workspace_files(self, workspace: Path) -> list[str]:
        """Files this run ACTUALLY produced so far — the partial evidence that
        must survive a failed method: preserved, listed, never re-established
        blindly and never laundered into completed-work claims."""
        root = Path(workspace)
        if not root.is_dir():
            return []
        return [str(p.relative_to(root)) for p in sorted(root.rglob("*"))
                if p.is_file() and p.relative_to(root).parts[0] not in ("prior", "input")][:20]

    def _method_outcome(self, stage: StageSpec, exc: Exception, workspace: Path) -> MethodOutcome:
        """Typed failure state (P0-A): classify WHAT failed and preserve what was
        learned, so recovery is adjudicated from evidence — failure phase, class,
        attempt history, retained observations — not from a bare retry count."""
        files = self._workspace_files(workspace)
        if isinstance(exc, AcquisitionError):
            return MethodOutcome(
                stage_id=stage.id, requirement=stage.goal, phase="acquisition",
                failure_class=exc.failure_class, detail=str(exc)[:500],
                capability=exc.need, attempts=exc.attempts,
                attempt_history=list(exc.history),
                retryable=exc.failure_class not in ("authority", "suppressed"),
                partial_evidence=files)
        return MethodOutcome(
            stage_id=stage.id, requirement=stage.goal, phase="execution",
            failure_class=("engine" if isinstance(exc, (TypeError, KeyError, AttributeError))
                           else "execution"),
            detail=f"{type(exc).__name__}: {str(exc)[:400]}",
            attempts=1, partial_evidence=files)

    async def _recover_failed_stage(self, failed: StageSpec, stages: list, i: int,
                                    objective: str, outputs: dict,
                                    outcome: MethodOutcome) -> bool:
        """Transactional failed-node replacement (P0-E/P0-F): a stage that never
        completed becomes a residual requirement and re-enters strategy
        adjudication — the same adjudicator that serves evidence gaps, so method
        selection is justified everywhere, never a domain branch. The failed
        method is suppressed for this recovery cycle unless the planner
        re-justifies it with a NEW contract; replacement stages are grafted in,
        dependents and any pending review are rewired to the new terminal
        producer, and the failed node survives only as failure history — never
        as evidence. An empty extension is a calibrated stop, finalized
        honestly. Bounded by RECOVERIES_PER_RUN."""
        extend = getattr(self.planner, "extend", None)
        if extend is None:
            return False
        if self._recoveries >= config.RECOVERIES_PER_RUN:
            self._recovery_state = (f"recovery budget exhausted "
                                    f"({config.RECOVERIES_PER_RUN} per run)")
            self.tracer.event("recovery.skipped", failed.id, status="failed",
                              reason=self._recovery_state)
            return False
        # Brain-decided persistence (Component 2): rather than a flat counter, the
        # supervisor judges — from the attempt history and the expectation gap —
        # whether trying another approach is still productive, or whether the run
        # has plateaued / needs something only the user can give. It can only make
        # the run stop EARLIER; the hard ceiling above remains the safety rail.
        if self.supervisor is not None:
            d = await self.supervisor.persistence(
                self._persistence_digest(failed, outcome))
            if d.action == "stop_and_ask":
                self._blocked = True
                self._help_request = {
                    "requirement": failed.goal,
                    "why": d.reason or "the viable approaches were explored without success",
                    "needed": d.message or "guidance, additional input, or access to proceed",
                    "tried": [f"{f.stage_id} [{f.phase}/{f.failure_class}]"
                              for f in self._failures[-6:]],
                    "retained": self._workspace_files(self._run_ctx[2]) if self._run_ctx else [],
                    "inputs": self._input_files(self._run_ctx[2]) if self._run_ctx else [],
                }
                self._recovery_state = "stopped to ask the user (approaches explored)"
                self.tracer.event("recovery.ask_user", failed.id, status="info",
                                  needed=str(d.message)[:200], reason=d.reason)
                return False
            if d.action == "stop":
                self._recovery_state = "calibrated stop (supervisor: no admissible path)"
                self.tracer.event("recovery.stopped", failed.id, status="info",
                                  reason=self._recovery_state)
                return False
            self.tracer.event("recovery.persist", failed.id, status="info",
                              reason=str(d.reason)[:200])
        self._recoveries += 1
        if outcome.capability:
            self._suppressed_caps.add(outcome.capability)
        need = {
            "need": (f"the objective still requires what stage '{failed.id}' was to "
                     f"establish: {failed.goal}"),
            "attempted": (f"method '{outcome.capability or failed.id}' FAILED during "
                          f"{outcome.phase} [{outcome.failure_class}] after "
                          f"{max(1, outcome.attempts)} attempt(s): {outcome.detail[:300]}"
                          + ("; attempt history: " + " | ".join(outcome.attempt_history[:4])
                             if outcome.attempt_history else "")),
        }
        completed = "\n".join(f"[{sid}] {(out or '')[:400]}"
                              for sid, out in outputs.items()) or "(none yet)"
        if outcome.partial_evidence:
            completed += ("\nPARTIAL OBSERVATIONS retained in the workspace (available to "
                          f"new stages via file tools): {outcome.partial_evidence[:10]}")
        try:
            result = await extend(objective, completed, [need], [s.id for s in stages])
        except Exception as e:  # noqa: BLE001 — recovery must never kill the run
            self._recovery_state = f"planner error during recovery ({type(e).__name__})"
            self.tracer.event("recovery.failed", failed.id, status="failed",
                              error=f"{type(e).__name__}: {str(e)[:200]}")
            return False
        new_stages, new_criteria, new_needs = self._unpack_extension(result)
        if new_criteria:
            self.tracer.event("plan.criteria.recompiled", "planner", status="info",
                              before=self._criteria, after=new_criteria)
            self._criteria = list(new_criteria)
        if new_needs:
            self._cap_needs.update(new_needs)
            # A fresh contract IS the new information that justifies retrying a
            # suppressed method — the planner made a deliberate, recorded choice.
            for key in new_needs:
                self._suppressed_caps.discard(key)
        if not new_stages:
            self._recovery_state = "no admissible alternative method (calibrated stop)"
            self.tracer.event("recovery.stopped", failed.id, status="info",
                              reason=self._recovery_state,
                              failure_class=outcome.failure_class)
            return False
        for off, s in enumerate(new_stages):
            stages.insert(i + 1 + off, s)
            self._stages_by_id[s.id] = s
        terminal = new_stages[-1].id
        # Rewire dependents: the failed node produced NOTHING, so its dependents'
        # true upstream is the replacement terminal; the superseded method stays
        # visible as failure history, never as evidence.
        for s in stages[i + 1 + len(new_stages):]:
            if failed.id in s.depends_on:
                s.depends_on = list(dict.fromkeys(
                    terminal if d == failed.id else d for d in s.depends_on))
        self._retarget_pending_review(stages, i + 1 + len(new_stages), new_stages)
        self._recovery_state = "replanned after method failure"
        self.tracer.event("plan.recovered", failed.id, status="info",
                          replaced_by=[s.id for s in new_stages], terminal=terminal,
                          failure_class=outcome.failure_class)
        return True

    def _persistence_digest(self, failed: StageSpec, outcome: MethodOutcome) -> str:
        """State for the supervisor's persistence judgment: what the failed stage
        must establish and its EXPECTED yield, the attempt history (is progress
        being made or has it plateaued?), what has actually been observed so far,
        and the remaining budget."""
        objective = self._run_ctx[0] if self._run_ctx else ""
        lines = [f"OBJECTIVE: {objective}", "",
                 f"A method for stage '{failed.id}' just FAILED "
                 f"[{outcome.phase}/{outcome.failure_class}].",
                 f"WHAT THIS STAGE MUST ESTABLISH: {failed.goal}"]
        if failed.expectation:
            lines.append(f"EXPECTED YIELD (what success looks like): {failed.expectation}")
        lines += ["", "ATTEMPT / FAILURE HISTORY THIS RUN "
                  "(judge PROGRESS — are attempts learning/closing the gap, or repeating?):"]
        for f in self._failures[-6:]:
            lines.append(f"- '{f.stage_id}' [{f.phase}/{f.failure_class}] "
                         f"x{max(1, f.attempts)}: {f.detail[:200]}")
            for h in f.attempt_history[:3]:
                lines.append(f"    · {h[:160]}")
        # Deterministic repeat detection: the same error signature under
        # different stage/tool names is the SAME approach failing — observed:
        # a supervisor read three identical failures as 'distinct fixable
        # bugs' because each rode in under a freshly-named contract.
        sigs: dict[str, int] = {}
        for entry in getattr(self, "_acq_failures", []) or []:
            sig = _failure_signature(str(entry).split(": ", 1)[-1])
            sigs[sig] = sigs.get(sig, 0) + 1
        repeats = sorted(((n, s) for s, n in sigs.items() if n >= 2), reverse=True)
        if repeats:
            lines.append("\nREPEATED ERROR SIGNATURES (deterministic engine count "
                         "across ALL tools/contracts this run — repetition without "
                         "new information is a PLATEAU, not a set of distinct bugs):")
            for n, s in repeats[:5]:
                lines.append(f"- seen {n}x: {s}")
        inputs = self._input_files(self._run_ctx[2]) if self._run_ctx else []
        if inputs:
            lines.append("\nUSER-PROVIDED INPUT FILES (present and readable in the run "
                         "workspace — the user does NOT need to re-provide these; a "
                         "failure to reach them is a method/engine problem, not a "
                         "missing-file problem): " + ", ".join(inputs))
        retained = self._workspace_files(self._run_ctx[2]) if self._run_ctx else []
        if retained:
            lines.append("\nPARTIAL OBSERVATIONS retained so far: " + ", ".join(retained[:10]))
        completed = getattr(self, "_outputs", None) or {}
        if completed:
            lines.append("COMPLETED STAGES: " + ", ".join(completed))
        lines.append(f"\nBUDGET: recoveries {self._recoveries}/{config.RECOVERIES_PER_RUN}; "
                     f"sessions {self._sessions}/{config.RUN_MAX_AGENT_SESSIONS}.")
        lines.append("Decide: keep_trying / stop_and_ask (state exactly what you need from the "
                     "user) / stop.")
        return "\n".join(lines)

    def _help_request_block(self) -> str:
        """The user-facing help request when the brain decides it needs input:
        how far it got, what it tried, why it stopped, and exactly what it needs."""
        h = self._help_request or {}
        retained = h.get("retained") or []
        lines = [
            "--- I need your input to continue ---",
            f"What I was trying to establish: {str(h.get('requirement', ''))[:300]}",
            "How far I got: " + (", ".join(retained[:8]) if retained
                                 else "no usable partial results were retained"),
            "What I tried: " + ("; ".join(h.get("tried") or []) or "(see the run trace)"),
            f"Why I stopped: {str(h.get('why', ''))[:300]}",
            f"What I need from you: {str(h.get('needed', ''))[:400]}",
        ]
        if h.get("inputs"):
            lines.insert(2, "Your provided files (already in the run, no re-upload "
                            "needed): " + ", ".join(h["inputs"][:8]))
        return "\n".join(lines)

    def _failure_report(self, workspace: Path) -> str:
        """Concise controller diagnosis for a run that completed nothing: what
        was tried (with the classified per-attempt history), what remains open,
        why execution stopped, and what partial evidence survives. The raw
        traceback stays in the trace and artifacts — it is diagnostics, not an
        answer."""
        lines = ["The objective could not be fulfilled: no stage completed and no "
                 "deliverable was produced.", "", "What was attempted:"]
        for f in self._failures:
            lines.append(
                f"- stage '{f.stage_id}'"
                + (f", capability '{f.capability}'" if f.capability else "")
                + f" — failed during {f.phase} [{f.failure_class}] after "
                  f"{max(1, f.attempts)} attempt(s): {f.detail[:240]}")
            lines += [f"    · {h[:200]}" for h in f.attempt_history[:4]]
        lines += ["", "What remains open:"]
        lines += [f"- {f.requirement[:220]}" for f in self._failures[:3]] or ["- (unrecorded)"]
        lines += ["", f"Why execution stopped: {self._recovery_state}."]
        retained = self._workspace_files(workspace)
        if retained:
            lines += ["", "Partial observations retained under workspace/: "
                          + ", ".join(retained[:10])]
        lines += ["", "Full diagnostics (per-attempt tool code, build logs, verification "
                      "output) are retained in this run's artifacts."]
        return "\n".join(lines)

    def _classify(self, had_failure: bool, workspace: Path,
                  last_done: Optional[str] = "") -> tuple[str, str]:
        """Map the run to succeeded | partial | unverified | blocked | failed,
        with the failing DIMENSION named precisely (#17/#20): grounding,
        artifact completeness, stage completion, and recovery are independent
        facts — 'grounded but truncated' must not read as a grounding failure.
        'not-assessed', 'not-required', and 'approved' are DIFFERENT states, and
        the reason never asserts an assessment that did not happen. EVERY
        expected terminal — including a first-stage failure where no agent ever
        started — flows through here, so dimensions and classification always
        exist and status, trace, and user message cannot disagree."""
        issues = artifact_issues(workspace, definite_only=True)  # only true-positive defects gate
        # Fatal blockers reported by agents are authoritative run state, not
        # side notes: 'I cannot observe what the objective asks about' must be
        # able to prevent SUCCEEDED.
        blockers = (self.blackboard.get("team.blockers", []) or []) if self.blackboard else []
        fatal = [b for b in blockers if b.get("severity") == "fatal"]
        if fatal:
            self._blocked = True
        nothing_completed = last_done is None
        dims = {
            "stage_completion": ("none-completed" if nothing_completed
                                 else "failed" if had_failure else "ok"),
            "grounding": ("approved" if self._review_approved
                          else "not-approved" if self._review_ran
                          else "not-required" if not self._high_risk else "missing"),
            "criteria": ("violated-measured" if self._format_violations
                         else "approved" if self._review_approved
                         else "assessed-not-approved" if self._review_ran
                         else "not-assessed" if self._criteria else "none-compiled"),
            "artifact": "defective" if issues else "ok",
            "blockers": "fatal" if fatal else ("open" if blockers else "none"),
            "recovery": self._recovery_state,
            "deliverable": "absent" if nothing_completed else "present",
            "method_failures": [f"{f.stage_id}:{f.phase}/{f.failure_class}"
                                for f in self._failures[:4]],
            # Certificate substance: how much retained evidence is OF the
            # designated source (target lineage) vs testimony about it. The
            # engine's own corpus manifest is bookkeeping ABOUT that evidence
            # and must not inflate the count.
            "target_evidence": len([
                e for e in evidence_mod.target_lineage(
                    evidence_mod.ledger(workspace, self._reader_dirs(),
                                        self._locators))
                if not str(e.file).startswith("corpus_manifest/")]),
        }
        self.tracer.event("run.dimensions", "run", status="info", **dims,
                          artifact_issues=issues[:6])
        # A brain-decided stop-and-ask is BLOCKED-needs-you, not a dead end —
        # the user can unblock it — so it outranks the nothing-completed 'failed'.
        if self._help_request:
            return "blocked", ("stopped to ask for your input — "
                               f"{str(self._help_request.get('needed', ''))[:200]}")
        if nothing_completed:
            tried = "; ".join(
                f"'{f.capability or f.stage_id}' ({f.phase}: {f.failure_class}"
                + (f", {f.attempts} attempt(s)" if f.attempts else "") + ")"
                for f in self._failures[:3]) or "no stage could start"
            return "failed", (f"no stage completed and no deliverable exists — "
                              f"failed method(s): {tried}; recovery: {self._recovery_state}.")
        if self._blocked:
            if self._help_request and not fatal:
                return "blocked", ("stopped to ask for your input — "
                                   f"{str(self._help_request.get('needed', ''))[:200]}")
            what = ("; ".join(f"{b.get('issue', '')}" for b in fatal[:2])
                    if fatal else "see the answer")
            return "blocked", f"the run hit an obstacle it could not resolve: {what}."
        if had_failure:
            return "partial", "a stage failed; the last completed result was delivered as-is."
        grounded = dims["grounding"] in ("approved", "not-required")
        if not grounded:
            why = ("its grounding review did not approve the content"
                   if self._review_ran else "a required grounding review did not run")
            tail = ("; the artifact ALSO has unresolved mechanical defects: "
                    + "; ".join(issues[:2])) if issues else ""
            return "unverified", f"unverified_grounding — {why}{tail}."
        if issues:
            return "partial", ("grounded_but_artifact_defective — the content is grounded, but "
                               "the artifact has unresolved mechanical defects: "
                               + "; ".join(issues[:3]))
        if self._format_violations:
            heads = "; ".join(f"required {v.op} {v.bound} {v.unit}(s), measured {v.measured}"
                              for v in self._format_violations[:3])
            return "partial", ("criteria_format_violation — engine-measured against the "
                               f"deliverable payload: {heads}. A reviewer verdict cannot "
                               "override a measured count.")
        if self._review_approved:
            return "succeeded", "grounding and success criteria approved by review."
        return "succeeded", ("operationally complete; no user-referenced evidence was "
                             "transformed (grounding not required)"
                             + (", and the compiled success criteria were NOT independently "
                                "verified" if self._criteria else "") + ".")

    # ------------------------------------------------------------------ #
    # stage dispatch
    # ------------------------------------------------------------------ #
    async def _run_stage(self, stage: StageSpec, stages: list[StageSpec],
                         outputs: dict[str, str], objective: str,
                         history: Optional[str], workspace: Path) -> str:
        if stage.pattern == MAP:
            return await self._run_map(stage, outputs, objective, history, workspace)
        if stage.pattern == REDUCE:
            return await self._run_reduce(stage, outputs, objective, history, workspace)
        if stage.pattern == REVIEW:
            return await self._run_review(stage, outputs, objective, history, workspace)
        return await self._run_single(stage, stages, outputs, objective, history, workspace)

    async def _run_single(self, stage: StageSpec, stages: list[StageSpec],
                          outputs: dict[str, str], objective: str,
                          history: Optional[str], workspace: Path) -> str:
        is_deliverable = stage.id in workflow.deliverable_stage_ids(
            list(self._stages_by_id.values()))
        # SELF-CHECK has teeth only if the producer can SEE its artifact the
        # way the audience will: give deliverable stages the promoted render
        # tool when one exists (reuse only — never stall production on
        # authoring; observed: a fix agent 'verified' a binary deck with a
        # text read and shipped the wrong slide count).
        if (is_deliverable and self.registry is not None
                and _RENDER_NEED_KEY not in stage.capabilities):
            try:
                have = self.registry.get(_RENDER_NEED_KEY)
            except Exception:  # noqa: BLE001 — a registry lookup must never fail a stage
                have = None
            if have is not None and have.verified and not have.quarantined:
                stage.capabilities.append(_RENDER_NEED_KEY)
        spec = AgentSpec(id=stage.id, role=stage.role, goal=stage.goal,
                         capabilities=stage.capabilities, depends_on=stage.depends_on)
        caps = await self._acquire_caps(spec)
        self._stage_caps[stage.id] = dict(caps)
        upstream = {d: outputs[d] for d in stage.depends_on if d in outputs}
        task_context = workflow.manifest_instruction() if workflow.feeds_a_map(stage.id, stages) else None
        if is_deliverable:
            task_context = (task_context or "") + workflow.deliverable_instruction()
            if self._knowledge_briefs:
                task_context += ("\n\nRESEARCHED QUALITY STANDARDS (the bar your "
                                 "deliverable is judged against — apply them):\n"
                                 + self._knowledge_briefs[:6000])
        out = await self._run_agent_with_reflection(
            spec, caps, objective, upstream, history, workspace, task_context=task_context)
        out = await self._maybe_repair_and_rerun(stage, spec, caps, out, objective,
                                                 upstream, history, workspace,
                                                 task_context)
        self._stage_notes[stage.id] = {stage.id: out}

        async def redo(critique: str, prior: str = "", _spec=spec, _caps=caps,
                       _up=upstream, _ctx=task_context) -> str:
            lessons = self._fix_notes.get(_spec.id, "")
            fix_ctx = ((_ctx or "")
                       + (f"\n\nYour previous draft:\n---\n{prior}\n---" if prior else "")
                       + (("\n\nEARLIER FIX ROUNDS' WORKING NOTES (operational lessons "
                           "about your tools and artifact — heed them, do not re-learn "
                           "them by repeating the mistake):\n" + lessons[:2500])
                          if lessons else "")
                       + "\n\nA reviewer requires these fixes — produce a corrected, "
                         "complete version of your result:\n" + critique)
            spec_fix = AgentSpec(id=f"{_spec.id}.fix", role=_spec.role, goal=_spec.goal,
                                 capabilities=_spec.capabilities, depends_on=_spec.depends_on)
            fixed = await self._run_agent_with_reflection(
                spec_fix, _caps, objective, _up, history, workspace, task_context=fix_ctx)
            notes = workflow.working_notes(fixed)
            if notes:
                self._fix_notes[_spec.id] = (lessons + "\n---\n" + notes)[-4000:]
            return fixed

        self._redo[stage.id] = redo
        if is_deliverable:
            out = await self._presubmit_format_pass(stage.id, out, redo, workspace)
        return out

    async def _presubmit_format_pass(self, stage_id: str, out: str, redo,
                                     workspace: Path) -> str:
        """Deterministic dry-run BEFORE review: measure the deliverable
        author's draft against the compiled count criteria and have the AUTHOR
        fix a measured violation immediately. A format violation is a
        deterministic fact the engine already holds at authoring time —
        spending a bounded, fully-gated review round to relay it is how two
        compliant-content runs shipped unverified. Slide/page units are
        excluded: their text heuristic only becomes authoritative through a
        render, which happens in review. Bounded per run; a fix attempt that
        measures WORSE is discarded (never trade a counted defect for more)."""
        if self._presubmit_fixes_left <= 0 or not self._criteria:
            return out
        gate, checks, target = self._count_gate(out, workspace)
        if not gate:
            return out
        self._presubmit_fixes_left -= 1
        facts = criteria_mod.describe(checks, target=target)
        self.tracer.event("criteria.presubmit_violation", stage_id, status="failed",
                          target=target,
                          violations=[f"{v.op} {v.bound} {v.unit}: measured {v.measured}"
                                      for v in gate])
        feedback = (
            "ENGINE PRE-REVIEW MEASUREMENT — your draft violates measured count "
            "constraints. Fix EXACTLY these violations and change nothing else "
            "(the measurements are deterministic facts about the deliverable of "
            "record named below, not reviewer opinions):\n" + facts)
        try:
            fixed = await redo(feedback, out)
        except Exception as e:  # noqa: BLE001 — a failed fixup leaves the draft standing
            self.tracer.event("stage.format_fixup", stage_id, status="failed",
                              error=f"{type(e).__name__}: {str(e)[:200]}")
            return out
        regate, _, _ = self._count_gate(fixed, workspace)
        if len(regate) <= len(gate):
            self.tracer.event("stage.format_fixup", stage_id, status="info",
                              before=len(gate), after=len(regate))
            self._stage_notes[stage_id] = {stage_id: fixed}
            return fixed
        self.tracer.event("stage.format_fixup", stage_id, status="failed",
                          note="fix attempt measured worse — original draft kept",
                          before=len(gate), after=len(regate))
        return out

    def _count_gate(self, text: str, workspace: Path) -> tuple[list, list, str]:
        """(gating count violations, all checks, measurement-target label) for
        a stage output. Slide/page units are excluded from the gate list: their
        text heuristic only becomes authoritative through a render."""
        payload = workflow.extract_deliverable(text) or text
        checks, target = self._measured_checks(payload, workspace)
        return ([v for v in criteria_mod.violations(checks)
                 if v.unit not in ("slide", "page")], checks, target)

    async def _remeasure_revision(self, draft_id: str, draft: str, redo,
                                  outputs: dict, workspace: Path) -> str:
        """Reviser discipline, deterministic: a fix round that satisfies the
        critique but INTRODUCES a measured count violation gets one immediate
        corrective pass — observed twice in one cycle: round-1 revisions fixed
        exactly what was critiqued and prepended process narration / appended
        a sources block, and the next bounded round was spent on defects the
        engine could measure the moment the revision landed. Never keeps a
        correction that measures worse; failures leave the revision standing."""
        if self._revision_fixups_left <= 0 or not self._criteria:
            return draft
        gate, checks, target = self._count_gate(draft, workspace)
        if not gate:
            return draft
        self._revision_fixups_left -= 1
        facts = criteria_mod.describe(checks, target=target)
        self.tracer.event("review.revision_remeasured", draft_id, status="failed",
                          target=target,
                          violations=[f"{v.op} {v.bound} {v.unit}: measured {v.measured}"
                                      for v in gate])
        msg = ("ENGINE RE-MEASUREMENT of your revision: it violates measured "
               "count constraints. A revision must satisfy ALL success criteria, "
               "not only the critiqued items — do not add headed process "
               "narration, source appendices, or any block the required "
               "structure does not include:\n" + facts
               + "\nFix EXACTLY these violations; preserve every fix you already made.")
        try:
            fixed = await redo(msg, draft)
        except Exception as e:  # noqa: BLE001 — the revision stands unfixed
            self.tracer.event("review.revision_fixup", draft_id, status="failed",
                              error=f"{type(e).__name__}: {str(e)[:200]}")
            return draft
        regate, _, _ = self._count_gate(fixed, workspace)
        if len(regate) <= len(gate):
            outputs[draft_id] = fixed
            self.tracer.event("review.revision_fixup", draft_id, status="info",
                              before=len(gate), after=len(regate))
            return fixed
        self.tracer.event("review.revision_fixup", draft_id, status="failed",
                          note="correction measured worse — revision kept as-is",
                          before=len(gate), after=len(regate))
        return draft

    async def _claims_gate_fixup(self, stage, rnd: int, void: str, claims: list,
                                 critique: str, lineage, objective: str,
                                 workspace: Path) -> tuple[str, list]:
        """Claims-gate dry-run exchange: the reviewer re-emits ONLY the claims
        table, told exactly why the current one would void and which
        target-lineage files exist to cite. Returns (void, claims) — cleared
        and corrected when the re-emitted table passes the UNCHANGED gate,
        original otherwise (a failed fix-up never softens the void, and a
        substituted table that voids for a new reason is treated as failed)."""
        self._gate_fixups_left -= 1
        files = sorted({e.file for e in lineage})[:24]
        prev = json.dumps(claims, indent=1)[:3000] if claims else "(no table was emitted)"
        spec = AgentSpec(
            id=f"{stage.id}.r{rnd}.gatefix", role=stage.role,
            goal="Re-emit the review's claim-support table with resolvable citations.",
            capabilities=[])
        ctx = (
            "CLAIMS-GATE DRY-RUN: your verdict was 'approved', but the engine's "
            f"deterministic claim-support check would VOID it: {void}\n\n"
            "Your verdict and critique stand — do NOT re-judge the draft. Re-emit "
            "ONLY the corrected fenced ```claims block (same JSON schema). Each "
            "'direct'/'derived'/'negative' claim must cite the exact target-lineage "
            "ledger file that supports it, from this list:\n"
            + "\n".join(f"- {f}" for f in files)
            + "\n\nBe honest, never cosmetic: retype a claim to 'inferred' or 'proxy' "
              "if no listed file supports it, and NEVER drop a load-bearing claim to "
              "pass the gate — the same gate re-checks your corrected table, and a "
              "table that fails again voids the approval exactly as before.\n\n"
              "Your previous table:\n" + prev
            + "\n\nYour critique, for reference:\n" + (critique or "")[:4000])
        try:
            fixed = await self._run_agent_with_reflection(
                spec, {}, objective, {}, None, workspace, task_context=ctx)
        except Exception as e:  # noqa: BLE001 — a dead fix-up leaves the void standing
            self.tracer.event("review.claims_fixup", stage.id, status="failed",
                              round=rnd, error=f"{type(e).__name__}: {str(e)[:200]}")
            return void, claims
        new_claims = _parse_claims(fixed)
        revoid = _claims_gate(new_claims, lineage) if new_claims else \
            "no claims table in the re-emission"
        self.tracer.event("review.claims_fixup", stage.id,
                          status="info" if not revoid else "failed",
                          round=rnd, resolved=not revoid, was=void[:160],
                          now=(revoid or "passed")[:160])
        if not revoid:
            return "", new_claims
        return void, claims

    async def _run_map(self, stage: StageSpec, outputs: dict[str, str], objective: str,
                       history: Optional[str], workspace: Path) -> str:
        source = stage.shard_from or (stage.depends_on[0] if stage.depends_on else "")
        items = workflow.parse_manifest(outputs.get(source, ""))
        if items is None:
            # Degrade honestly: no machine-readable manifest -> one worker over the raw output.
            self.tracer.event("map.no_manifest", stage.id, status="failed", source=source)
            items = [outputs.get(source, "")] if outputs.get(source) else []
        if not items:
            self.tracer.event("map.empty", stage.id, status="failed", source=source)
            return f"(map stage '{stage.id}': upstream '{source}' produced no work items)"

        shards = workflow.shard(items, stage.shard_size or config.MAP_DEFAULT_SHARD_SIZE,
                                config.MAP_MAX_WORKERS_PER_STAGE)
        self.tracer.event("map.fanout", stage.id, status="info",
                          items=len(items), workers=len(shards),
                          shard_size=max(len(s) for s in shards), source=source)

        # Acquire the worker template's capabilities ONCE, before fan-out, so
        # parallel workers never race to build the same tool.
        template = AgentSpec(id=stage.id, role=stage.role, goal=stage.goal,
                             capabilities=stage.capabilities, depends_on=[])
        caps = await self._acquire_caps(template)

        sem = asyncio.Semaphore(max(1, config.WORKER_PARALLELISM))
        total = len(shards)

        async def run_worker(k: int, shard_items: list, attempt: int = 1) -> str:
            wid = f"{stage.id}.w{k + 1:02d}" + (f".r{attempt}" if attempt > 1 else "")
            spec = AgentSpec(id=wid, role=stage.role, goal=stage.goal,
                             capabilities=stage.capabilities, depends_on=[])
            listing = "\n".join(f"- {workflow.render_item(it)}" for it in shard_items)
            task_context = (
                f"Your assigned work items (group {k + 1} of {total}; other workers handle "
                f"the rest — work ONLY on these, in depth):\n{listing}\n\n"
                "Return your findings as structured notes: per item, what you established, "
                "with evidence (quote or cite the source/location). Flag anything you could "
                "not verify. Do not pad; depth over breadth."
            )
            async with sem:
                return await self._run_agent_with_reflection(
                    spec, caps, objective, {}, history, workspace, task_context=task_context,
                    model=config.model_for("worker"))

        results = await asyncio.gather(
            *(run_worker(k, s) for k, s in enumerate(shards)), return_exceptions=True)

        notes: dict[str, str] = {}
        dead: list[int] = []
        for k, res in enumerate(results):
            wid = f"{stage.id}.w{k + 1:02d}"
            if isinstance(res, BaseException):
                dead.append(k)
                self.tracer.event("worker.failed", wid, status="failed", error=str(res)[:300])
                notes[wid] = f"(worker failed: {str(res)[:300]})"
            else:
                notes[wid] = res

        # A person re-assigns a sick colleague's pile: failed shards get one
        # fresh session before the stage gives up on their items.
        for attempt in range(2, config.MAP_RETRY_FAILED_SHARDS + 2):
            if not dead:
                break
            self.tracer.event("map.retry", stage.id, status="info",
                              attempt=attempt, shards=[k + 1 for k in dead])
            retries = await asyncio.gather(
                *(run_worker(k, shards[k], attempt=attempt) for k in dead),
                return_exceptions=True)
            still_dead: list[int] = []
            for k, res in zip(dead, retries):
                wid = f"{stage.id}.w{k + 1:02d}"
                if isinstance(res, BaseException):
                    still_dead.append(k)
                    self.tracer.event("worker.failed", f"{wid}.r{attempt}",
                                      status="failed", error=str(res)[:300])
                else:
                    notes[wid] = res
            dead = still_dead

        failed = len(dead)
        self._stage_notes[stage.id] = notes
        if failed == len(shards):
            return f"(map stage '{stage.id}': all {failed} workers failed)"

        digest = (f"[map:{stage.id}] {len(shards)} workers processed {len(items)} items "
                  f"({failed} failed). Full notes are in this run's outputs/, one file per worker.")
        return digest

    async def _run_reduce(self, stage: StageSpec, outputs: dict[str, str], objective: str,
                          history: Optional[str], workspace: Path) -> str:
        notes: dict[str, str] = {}
        for dep in stage.depends_on:
            notes.update(self._stage_notes.get(dep) or
                         ({dep: outputs[dep]} if dep in outputs else {}))
        if not notes:
            return f"(reduce stage '{stage.id}': nothing to consolidate)"

        limit = config.REDUCE_MAX_INPUT_CHARS
        batches = _batch_notes(notes, limit)
        if len(batches) > 1:
            # Hierarchical consolidation: independent partial merges run in
            # bounded parallel (same width as map workers), then the final one.
            self.tracer.event("reduce.batched", stage.id, status="info", batches=len(batches))
            sem = asyncio.Semaphore(max(1, config.WORKER_PARALLELISM))

            async def run_partial(j: int, batch: dict[str, str]) -> tuple[str, str]:
                bid = f"{stage.id}.b{j + 1:02d}"
                spec = AgentSpec(id=bid, role=stage.role,
                                 goal=("Partial consolidation towards: " + stage.goal +
                                       " Merge ONLY the notes given to you, faithfully; keep every "
                                       "source attribution; do not invent or drop findings."))
                ctx = "Notes to consolidate:\n" + _notes_block(batch, limit)
                async with sem:
                    out = await self._run_agent_with_reflection(
                        spec, {}, objective, {}, history, workspace, task_context=ctx)
                return bid, out

            done = await asyncio.gather(*(run_partial(j, b) for j, b in enumerate(batches)))
            notes = dict(done)

        spec = AgentSpec(id=stage.id, role=stage.role, goal=stage.goal,
                         capabilities=stage.capabilities, depends_on=stage.depends_on)
        caps = await self._acquire_caps(spec)
        ctx = ("Notes from the workers (merge faithfully; keep source attributions; "
               "flag disagreements between workers rather than papering over them):\n"
               + _notes_block(notes, limit))
        if stage.id in workflow.deliverable_stage_ids(list(self._stages_by_id.values())):
            ctx += workflow.deliverable_instruction()
            if self._knowledge_briefs:
                ctx += ("\n\nRESEARCHED QUALITY STANDARDS (the bar your deliverable is "
                        "judged against — apply them):\n" + self._knowledge_briefs[:6000])
        out = await self._run_agent_with_reflection(
            spec, caps, objective, {}, history, workspace, task_context=ctx)
        out = await self._maybe_repair_and_rerun(stage, spec, caps, out, objective,
                                                 {}, history, workspace, ctx)
        self._stage_notes[stage.id] = {stage.id: out}

        async def redo(critique: str, prior: str = "", _spec=spec, _caps=caps, _ctx=ctx) -> str:
            lessons = self._fix_notes.get(_spec.id, "")
            fix_ctx = (_ctx
                       + (f"\n\nYour previous draft:\n---\n{prior}\n---" if prior else "")
                       + (("\n\nEARLIER FIX ROUNDS' WORKING NOTES (operational lessons "
                           "about your tools and artifact — heed them, do not re-learn "
                           "them by repeating the mistake):\n" + lessons[:2500])
                          if lessons else "")
                       + "\n\nA reviewer requires these fixes — produce a corrected, "
                         "complete version of the deliverable:\n" + critique)
            spec_fix = AgentSpec(id=f"{_spec.id}.fix", role=_spec.role, goal=_spec.goal,
                                 capabilities=_spec.capabilities, depends_on=_spec.depends_on)
            fixed = await self._run_agent_with_reflection(
                spec_fix, _caps, objective, {}, history, workspace, task_context=fix_ctx)
            notes = workflow.working_notes(fixed)
            if notes:
                self._fix_notes[_spec.id] = (lessons + "\n---\n" + notes)[-4000:]
            return fixed

        self._redo[stage.id] = redo
        if stage.id in workflow.deliverable_stage_ids(list(self._stages_by_id.values())):
            out = await self._presubmit_format_pass(stage.id, out, redo, workspace)
        return out

    async def _run_review(self, stage: StageSpec, outputs: dict[str, str], objective: str,
                          history: Optional[str], workspace: Path) -> str:
        if not stage.depends_on:
            return f"(review stage '{stage.id}': nothing to review)"
        draft_id = workflow.review_subject(stage, list(self._stages_by_id.values()))
        draft = outputs.get(draft_id, "")

        # Evidence: the draft stage's underlying notes (its dependencies' outputs),
        # plus any additional dependencies the planner attached to the review.
        evidence: dict[str, str] = {}
        draft_stage = self._stages_by_id.get(draft_id)
        if draft_stage is not None:
            for dep in draft_stage.depends_on:
                evidence.update(self._stage_notes.get(dep) or {})
        for dep in stage.depends_on:
            if dep == draft_id:
                continue
            evidence.update(self._stage_notes.get(dep) or
                            ({dep: outputs[dep]} if dep in outputs else {}))

        limit = config.REDUCE_MAX_INPUT_CHARS
        verdict = "revise"
        crash_note = ""           # carried into the NEXT round after a crash
        couriers_left = config.REVIEW_EVIDENCE_FETCHES
        self._review_ran = True
        record: list[dict] = []   # authoritative per-round verdict record
        criteria_block = (
            "\n\nSUCCESS CRITERIA (compiled from the user's objective — check the deliverable "
            "against EACH; an unmet criterion is grounds to revise):\n"
            + "\n".join(f"- {c}" for c in self._criteria) if self._criteria else "")
        evidence_server = {"evidence": self._evidence_server(workspace)}
        evidence_tools = ["mcp__evidence__locate_evidence"]
        # Round accounting: content verdicts spend the budget; an
        # infrastructure-crashed round may be retried REVIEW_CRASH_RETRIES
        # times without cost (observed: a transport fault consumed a 1-round
        # review and an excellent draft shipped unverified, its review never
        # actually performed).
        rounds_budget = max(1, stage.rounds)
        crash_retries = max(0, config.REVIEW_CRASH_RETRIES)
        post_redos_left = max(0, config.POST_REVIEW_REDOS)
        self._revision_fixups_left = max(0, config.REVISION_FORMAT_FIXUPS)
        self._gate_fixups_left = max(0, config.REVIEW_GATE_FIXUPS)
        spent = 0
        try:
            for rnd in range(1, rounds_budget + crash_retries + post_redos_left + 1):
                if spent >= rounds_budget:
                    break
                spent += 1
                # PERCEPTUAL REVIEW: if an audience will look at the artifact,
                # the reviewer must look at it. Renderable formats get the
                # render capability (authored on first use, like any tool).
                renderable, image_artifacts = self._perceivable_artifacts(workspace)
                if renderable and _RENDER_NEED_KEY not in stage.capabilities:
                    self._cap_needs.setdefault(_RENDER_NEED_KEY,
                                               {"contract": _RENDER_CONTRACT,
                                                "example_input": {"files": [renderable[0]]}})
                    stage.capabilities.append(_RENDER_NEED_KEY)
                spec = AgentSpec(id=f"{stage.id}.r{rnd}", role=stage.role, goal=stage.goal,
                                 capabilities=stage.capabilities, depends_on=stage.depends_on)
                # A reviewer's tooling failing to BUILD must not kill the
                # review wholesale (observed: a reader tool failed authoring
                # and the whole review errored, shipping the draft with no
                # substantive review at all). Degrade: review with available
                # means, the gap stated in context.
                caps, degraded_note = await self._acquire_caps_best_effort(spec)
                observed = _observed_artifacts(workspace)
                ledger_entries = evidence_mod.ledger(workspace, self._reader_dirs(),
                                                     self._locators)
                # Fresh corpus manifest each round (couriers may have retained
                # more since the last one) — then re-ledger so the manifest is
                # itself visible/citable this round.
                manifest_rel = self._write_corpus_manifest(workspace, ledger_entries)
                if manifest_rel:
                    ledger_entries = evidence_mod.ledger(workspace, self._reader_dirs(),
                                                         self._locators)
                lineage_paths = [e.file for e in
                                 evidence_mod.target_lineage(ledger_entries)]
                source = _source_evidence(workspace, self._reader_dirs(),
                                          extra_paths=lineage_paths)
                _art_issues = artifact_issues(workspace)
                inp = workspace / "input"
                evidence_expected = self._high_risk or (inp.is_dir() and any(inp.iterdir()))
                no_source = ("" if source or not evidence_expected else
                             "NO ORIGINAL SOURCE EVIDENCE could be assembled, although this run "
                             "derives claims from user-referenced sources (attached files and/or "
                             "remote locators). You cannot approve grounding you cannot see, and "
                             "reasoning over upstream narration instead is exactly the failure "
                             "this block exists to prevent: your verdict MUST be "
                             "'needs-evidence' (name the observation/extraction needed — a "
                             "courier will fetch and RETAIN it) or 'revise' — never 'approved'.\n\n")
                # The deliverable PAYLOAD is what ships — judge it, not the
                # stage's narration around it; countable criteria arrive as
                # engine measurements, not eyeballing.
                payload = workflow.extract_deliverable(draft) or draft
                payload_note = ("" if payload == draft else
                                "\n(The stage's working notes outside the fenced deliverable "
                                "block were stripped — THIS payload is what ships and what "
                                "format criteria are measured against.)")
                rendered_pages = self._rendered_page_count(workspace)
                checks, meas_target = self._measured_checks(payload, workspace)
                facts = criteria_mod.describe(checks, target=meas_target)
                if rendered_pages is not None:
                    if self._render_is_stale(workspace):
                        facts += (("\n" if facts else "")
                                  + "ARTIFACT RENDER IS STALE: the artifact was "
                                    "REGENERATED after the last render — the page images "
                                    "depict a SUPERSEDED draft. RE-RENDER before judging; "
                                    "page counts are unmeasurable until then, and an "
                                    "approval over stale perception will be voided.")
                    else:
                        facts += (("\n" if facts else "")
                                  + f"ARTIFACT RENDER: the latest render produced "
                                    f"{rendered_pages} page image(s) — authoritative for "
                                    "slide/page counts.")
                perceptual_block = ""
                if renderable or image_artifacts:
                    perceptual_block = (
                        "PERCEIVABLE ARTIFACTS this run produced (an audience will LOOK "
                        "at these — you must too):\n"
                        + "".join(f"- {f}\n" for f in (renderable + image_artifacts))
                        + ("RENDER + LOOK: call render_artifact_pages on the artifact, "
                           "then OPEN EVERY rendered page with read_file. "
                           if renderable else
                           "LOOK: open every artifact image with read_file. ")
                        + "Judge the CRAFT against the success criteria and any "
                          "researched standards: layout integrity (no overflow, "
                          "clipping, or collisions), data-graphic integrity (honest "
                          "axes, one visual encoding per series, readable labels), "
                          "consistency (type, color, alignment), and audience-copy "
                          "discipline (no editorial notes, placeholders, or "
                          "meta-references in what the audience reads). Craft defects "
                          "are grounds to revise — a deliverable can be grounded and "
                          "still fail its audience. Slide/page counts are measured "
                          "from the render by the engine; a mismatch voids approval.\n\n")
                ledger_block = evidence_mod.describe(ledger_entries, self._locators)
                ctx = (
                    "DRAFT under review (the deliverable payload):\n---\n" + payload
                    + "\n---" + payload_note + "\n\n"
                    + (degraded_note + "\n\n" if degraded_note else "")
                    + (crash_note + "\n\n" if crash_note else "")
                    + ledger_block + "\n\n"
                    + (f"CORPUS MANIFEST — engine-generated complete enumeration of the "
                       f"retained target-lineage evidence; the citable ground for a claim "
                       f"that something is ABSENT from the sources: {manifest_rel}\n\n"
                       if manifest_rel else "")
                    + (facts + "\n\n" if facts else "")
                    + perceptual_block
                    + (f"ORIGINAL SOURCE EVIDENCE (the user's actual inputs / raw extraction — the "
                       f"ground truth every claim must ultimately trace to):\n{source}\n\n"
                       if source else "")
                    + no_source
                    + ("UPSTREAM ANALYSIS (an intermediate transformation — do NOT treat this as "
                       "ground truth; it may itself contain unsupported claims):\n"
                       + _notes_block(evidence, limit) + "\n\n" if evidence else "")
                    + (f"OBSERVED ARTIFACT FILES actually produced this run (judge THESE, not the "
                       f"draft's self-description of them):\n{observed}\n\n" if observed else "")
                    + criteria_block
                    + (f"ARTIFACT ISSUES the engine detected ([definite] must be fixed; "
                       f"[suspected] is an unverified hint — judge it in context, an "
                       f"unpunctuated caption is NOT a defect):\n"
                       f"{chr(10).join('- ' + i for i in _art_issues)}\n\n" if _art_issues else "")
                    + "\nTRACE THE EVIDENCE CHAIN: artifact claim -> upstream analysis -> ORIGINAL "
                      "SOURCE EVIDENCE. A claim that appears in the analysis but is NOT supported by "
                      "the original source evidence is UNSUPPORTED even though an upstream stage "
                      "asserted it — flag it and REVISE. Verify the observed artifact matches what "
                      "the draft claims about it (counts, length, described features must actually be "
                      "present). CROSS-DERIVE NUMERIC CLAIMS: when the sources' structured data can "
                      "be combined into the same quantity a claim states (a rate times its period, a "
                      "sum of parts, a count of records), COMPUTE it and compare — 'no source "
                      "contradicts this figure' is assertable only after the derivable checks, and "
                      "an unexplained mismatch is a discrepancy the deliverable must surface. SOURCE IDENTITY: the deliverable's subject/title/central themes must "
                      "match the ACTUAL source (check the source's real title and recurring terms); a "
                      "renamed source or themes/keywords absent from the source evidence (e.g. safety "
                      "framing imported from background knowledge) are fabrication -> REVISE. Content "
                      "marked NON-EVIDENCE or from ignored inputs must not appear as source claims. "
                      "APPROVE only when load-bearing claims trace to the ORIGINAL SOURCE, the source "
                      "identity is consistent, and the criteria are met — style is NOT grounds for "
                      "revision. "
                      "ADJUDICATE BY LOOKUP: before declaring any specific claim (a number, "
                      "quotation, name, title) unverified or fabricated, call locate_evidence with "
                      "the exact string — FOUND means grounded (cite the location); NOT FOUND after "
                      "lookup means unsupported. Never settle a claim's presence by asking the stage "
                      "that produced it: a follow-up agent answers from memory, not the files. "
                      "EVIDENCE TIERS: ground your verdict in the RETAINED EVIDENCE LEDGER — "
                      "'target'-tier entries are observation of the designated source; "
                      "'proxy'-tier entries are retained testimony ABOUT it (grounding in proxy "
                      "alone is a stated limitation, only acceptable when the criteria admit it); "
                      "an agent's or courier's PROSE — however organized or confident — is "
                      "narration, NEVER evidence, whatever it calls itself. Use read_file to "
                      "open retained files (images are shown as images — look at visual artifacts "
                      "before judging claims about them). "
                      "Your FIRST line must be exactly one of: 'VERDICT: approved' — "
                      "'VERDICT: revise' (problems fixable from the existing evidence) — "
                      "'VERDICT: needs-evidence' (a load-bearing claim can only be settled by fetching "
                      "a specific missing source; then list EXACTLY what to fetch). A load-bearing "
                      "source fact that is UNKNOWN from the evidence shown (e.g. the source's actual "
                      "title/identity) is exactly the 'needs-evidence' case. If revising, follow "
                      "with a numbered list of specific, actionable fixes. "
                      "AFTER the verdict line, emit a fenced ```criteria block: a JSON array with "
                      "one entry PER SUCCESS CRITERION — {\"criterion\": \"...\", \"status\": "
                      "\"met\"|\"unmet\"|\"unknown\", \"evidence\": \"the ledger file/lookup/"
                      "measurement that settles it, or why it is unknown\"}. A criterion you "
                      "cannot ground in retained evidence or an engine measurement is 'unknown', "
                      "never 'met'. "
                      "THEN emit a fenced ```claims block: a JSON array covering the "
                      "deliverable's LOAD-BEARING claims — {\"claim\": \"...\", \"support\": "
                      "\"direct\"|\"derived\"|\"inferred\"|\"negative\"|\"proxy\"|"
                      "\"unsupported\", \"evidence\": \"the exact ledger file that supports "
                      "it\"}. Type honestly: 'direct'/'derived' ONLY when a target-lineage "
                      "ledger file you actually opened or located supports the claim (cite "
                      "that file); an event asserted BETWEEN observations (motion, "
                      "transitions, causation bridging samples) is 'inferred', not direct; "
                      "'negative' (an absence claim) requires observed_absent evidence OR — "
                      "for absence WITHIN the designated corpus — a citation of the corpus "
                      "manifest file, whose complete enumeration is the observation that "
                      "grounds within-corpus absence; not_detected or observation_failed "
                      "NEVER support absence, demand qualified wording instead; 'proxy' "
                      "means only testimony about the target supports it. The engine deterministically checks this table: "
                      "an approval with unsupported claims, miscited lineage, or no "
                      "direct/derived support at all will be VOIDED."
                )
                crash_note = ""   # consumed — it is baked into this round's ctx
                try:
                    critique = await self._run_agent_with_reflection(
                        spec, caps, objective, {}, history, workspace, task_context=ctx,
                        extra_servers=evidence_server, extra_tools=evidence_tools)
                except Exception as e:  # noqa: BLE001 — a crashed round costs at most ONE
                    # round, never the whole review (observed: an oversized
                    # perception result killed the final round's session and the
                    # draft shipped UNVERIFIED with a precise defect list
                    # unapplied); the first crash(es) cost NOTHING — an
                    # infrastructure death is not a content verdict.
                    err = f"{type(e).__name__}: {str(e)[:300]}"
                    refunded = crash_retries > 0
                    if refunded:
                        crash_retries -= 1
                        spent -= 1
                    self.tracer.event("review.round_failed", stage.id, status="failed",
                                      round=rnd, error=err, round_refunded=refunded)
                    record.append({"round": rnd, "verdict": "errored", "error": err})
                    self._review_verdicts.append("errored")
                    verdict = "errored"
                    crash_note = (
                        f"NOTE: review round {rnd} CRASHED mid-session ({err})"
                        + (" — retried without consuming the round budget."
                           if refunded else " and consumed one round.")
                        + " The usual cause is an oversized perception step — work "
                        "economically this round: open ONE file per call, rely on the "
                        "engine-measured facts already in this context instead of "
                        "re-opening everything, and reach the verdict protocol early.")
                    continue
                m = _VERDICT_RE.search(critique or "")
                # An UNPARSEABLE verdict is a protocol violation, not an
                # approval: default to 'revise', never 'approved' (observed: a
                # synthesis grounded in ZERO observed frames was auto-approved
                # because the reviewer's prose lacked the exact verdict token).
                verdict = m.group(1).lower() if m else "revise"
                if not m:
                    self.tracer.event("review.verdict.unparsed", stage.id, status="failed",
                                      round=rnd, note="no VERDICT line -> defaulted to revise")
                per_criterion = _parse_criteria_verdicts(critique)
                claims = _parse_claims(critique)

                # DETERMINISTIC APPROVAL GATE: a parsed 'approved' cannot
                # override the engine's own facts. Observed failures this
                # gate closes: approval on narration over an empty ledger
                # (V060), and approval whose formal ledger contained no
                # target-lineage support at all — a hollow certificate over a
                # real success (V0100). Facts that void approval on a
                # source-derived run: no target-lineage evidence, a missing or
                # failing claim-support table, a miscited lineage claim, or a
                # measured violation of a countable criterion.
                void = ""
                absence_void = False   # evidence-ABSENCE voids are not author-fixable
                if verdict == "approved":
                    fresh = evidence_mod.ledger(workspace, self._reader_dirs(),
                                                self._locators)
                    if self._source_derived and not fresh:
                        void = ("empty evidence ledger: this run derives claims from "
                                "designated sources but NO payload was retained — "
                                "narration was approved, not evidence")
                        absence_void = True
                    elif self._source_derived and not evidence_mod.target_lineage(fresh):
                        void = ("no target-lineage evidence: every retained payload is "
                                "proxy-tier or unknown lineage — testimony ABOUT the "
                                "designated source cannot certify claims about the "
                                "source itself")
                        absence_void = True
                    elif self._source_derived:
                        void = _claims_gate(claims, evidence_mod.target_lineage(fresh))
                        # CLAIMS-GATE DRY-RUN: a fixable table-mechanics defect
                        # (see _FIXABLE_GATE_PREFIXES) surfaces to the reviewer
                        # as a correctable warning ONCE before it voids —
                        # observed: an approval earned on the merits was voided
                        # because one claim's citation named the notes ABOUT
                        # the verified frames instead of the frames' ledger
                        # file. The gate itself is unchanged; the corrected
                        # table passes the SAME gate or the original void lands.
                        if void and void.startswith(_FIXABLE_GATE_PREFIXES) and \
                                self._gate_fixups_left > 0:
                            void, claims = await self._claims_gate_fixup(
                                stage, rnd, void, claims, critique,
                                evidence_mod.target_lineage(fresh), objective,
                                workspace)
                    # PERCEPTION FRESHNESS: an approval must be grounded in
                    # perception OF the artifact under judgment. Renderable
                    # artifact + available render tool + no render, or a
                    # render the artifact has since outrun => void.
                    if not void and renderable:
                        if self._render_is_stale(workspace):
                            void = ("stale perception: the artifact was regenerated "
                                    "AFTER the render this review relied on — "
                                    "re-render and judge the CURRENT artifact")
                        elif (self._rendered_page_count(workspace) is None
                              and _RENDER_NEED_KEY in caps):
                            void = ("unperceived artifact: an audience-facing "
                                    "renderable artifact exists and the render tool is "
                                    "available, but no render was ever made — render "
                                    "it and LOOK at every page before approving")
                    if not void:
                        gate_checks, gate_target = self._measured_checks(
                            workflow.extract_deliverable(draft) or draft, workspace)
                        gate = criteria_mod.violations(gate_checks)
                        if gate:
                            v = gate[0]
                            # The reviewer renders DURING the round: re-read.
                            # _measured_checks drops payload slide/page checks
                            # whenever a render exists, so a surviving one IS
                            # a render measurement.
                            where = ("in the RENDERED artifact"
                                     if v.unit in ("slide", "page")
                                     and self._rendered_page_count(workspace)
                                     is not None
                                     else f"in {gate_target}")
                            void = (f"engine-measured format violation: required {v.op} "
                                    f"{v.bound} {v.unit}(s), measured {v.measured} {where}")
                if void:
                    self.tracer.event("review.approval.voided", stage.id, status="failed",
                                      round=rnd, reason=void)
                    verdict = "needs-evidence" if (
                        couriers_left > 0 and ("ledger" in void or "lineage" in void)
                    ) else "revise"
                    critique += (
                        f"\n\n[ENGINE GATE] The 'approved' verdict was VOIDED: {void}. "
                        "Approval requires the load-bearing claims to trace — via the "
                        "```claims table — to RETAINED target-lineage evidence files "
                        "(tools must retain payloads; prose does not count), and the "
                        "deliverable payload to satisfy every measured count constraint.")
                    record.append({"round": rnd, "verdict": "approved-voided",
                                   "criteria": per_criterion, "claims": claims,
                                   "critique": (critique or "").strip()[:4000]})
                    self._review_verdicts.append("approved-voided")
                else:
                    record.append({"round": rnd, "verdict": verdict,
                                   "criteria": per_criterion, "claims": claims,
                                   "critique": (critique or "").strip()[:4000]})
                    self._review_verdicts.append(verdict)
                self.tracer.event("review.verdict", stage.id, status="info",
                                  round=rnd, verdict=self._review_verdicts[-1],
                                  parsed=bool(m), criteria=len(per_criterion))
                if verdict == "approved":
                    self._review_approved = True
                    self._write_review_record(stage.id, record, "approved")
                    return draft

                extra = ""
                if verdict == "needs-evidence" and couriers_left > 0:
                    couriers_left -= 1
                    courier = AgentSpec(
                        id=f"{stage.id}.evidence", role="Evidence courier",
                        goal="Obtain EXACTLY the missing evidence the reviewer listed, using "
                             "tools that RETAIN payloads (retained files are the deliverable "
                             "of this errand — your prose is not evidence). Fetch nothing else.",
                        capabilities=self._evidence_capabilities(draft_stage))
                    before = {e.file for e in evidence_mod.ledger(
                        workspace, self._reader_dirs(), self._locators)}
                    courier_caps, courier_note = \
                        await self._acquire_caps_best_effort(courier)
                    try:
                        with self.tracer.span("review.evidence", stage.id):
                            fetched = await self._run_agent_with_reflection(
                                courier, courier_caps, objective, {},
                                history, workspace,
                                task_context=(courier_note + "\n\n" if courier_note else "")
                                + "The reviewer needs this evidence:\n" + critique,
                                extra_servers=evidence_server, extra_tools=evidence_tools)
                    except Exception as e:  # noqa: BLE001 — courier death ≠ review death
                        self.tracer.event("courier.failed", stage.id, status="failed",
                                          error=f"{type(e).__name__}: {str(e)[:200]}")
                        fetched = (f"(the evidence courier crashed: {type(e).__name__}: "
                                   f"{str(e)[:200]} — no new evidence was fetched)")
                    evidence[courier.id] = fetched
                    # Label the courier's text by the LEDGER's fact, not the
                    # courier's own account: the engine calling narration
                    # "NEWLY FETCHED EVIDENCE" was itself a laundering channel.
                    gained = sorted({e.file for e in evidence_mod.ledger(
                        workspace, self._reader_dirs(), self._locators)} - before)
                    if gained:
                        extra = ("\n\nNEWLY RETAINED EVIDENCE (courier; files: "
                                 + ", ".join(gained[:8]) + "):\n" + fetched[:20000])
                    else:
                        self.tracer.event("courier.ineffective", stage.id, status="failed",
                                          note="courier retained no files — report is narration")
                        extra = ("\n\nCOURIER REPORT (the courier retained NO evidence "
                                 "files — this is narration, NOT evidence; claims cannot "
                                 "be grounded in it):\n" + fetched[:20000])

                redo = self._redo.get(draft_id)
                if redo is None:
                    break
                if spent >= rounds_budget:
                    # POST-REVIEW REDO, bounded: the budget is exhausted and
                    # the reviewer's final verdict holds a precise, actionable
                    # critique that would otherwise never be applied (observed
                    # three times in one cycle as supervisor redo orders that
                    # could not run). Apply it once and buy exactly ONE more
                    # fully-gated round — every void/gate applies to it. An
                    # evidence-ABSENCE void is excluded: no author redo can
                    # retain evidence, so the round would be spent re-voiding.
                    if post_redos_left <= 0 or verdict != "revise" or absence_void:
                        break
                    post_redos_left -= 1
                    rounds_budget += 1
                    self.tracer.event("review.post_redo", draft_id, status="info",
                                      round=rnd,
                                      note="review exhausted holding an actionable "
                                           "critique — applying it and granting one "
                                           "fully-gated round")
                try:
                    draft = await redo(critique + extra, draft)
                    outputs[draft_id] = draft
                    self.tracer.event("review.revised", draft_id, status="info", round=rnd)
                except Exception as e:  # noqa: BLE001 — a crashed fix round leaves the
                    # previous draft standing; the next round re-reviews it.
                    self.tracer.event("review.revision_failed", draft_id, status="failed",
                                      round=rnd, error=f"{type(e).__name__}: {str(e)[:200]}")
                    crash_note = (f"NOTE: the fix round after round {rnd} CRASHED "
                                  f"({type(e).__name__}) — the draft is UNCHANGED; your "
                                  "previous critique was not applied.")
                else:
                    draft = await self._remeasure_revision(draft_id, draft, redo,
                                                           outputs, workspace)
        except Exception as e:  # noqa: BLE001
            # The reviewer dying must not cost the user the draft: deliver it
            # unverified, honestly labeled.
            self.tracer.event("review.failed", stage.id, status="failed",
                              error=f"{type(e).__name__}: {str(e)[:300]}")
            self._write_review_record(
                stage.id, record, f"errored ({type(e).__name__}) — draft delivered UNVERIFIED")
            return (draft + f"\n\n[engine note] the verification stage failed "
                            f"({type(e).__name__}: {str(e)[:200]}); this draft is delivered UNVERIFIED.")

        self.tracer.event("review.exhausted", stage.id, status="failed", rounds=stage.rounds)
        # Escalate past the review's own toolbox: an unresolved evidence demand
        # on a source-derived run with an EMPTY ledger is an unmet OBSERVATION
        # need — the same signal agents report — so the planner can author
        # retention-capable observation the courier structurally lacked (the
        # V060 courier could only re-run the very tools whose insufficiency
        # caused the demand). Bounded by REPLANS_PER_RUN like every replan.
        if self.blackboard is not None and self._source_derived and \
                not evidence_mod.ledger(workspace, self._reader_dirs(), self._locators):
            gaps = self.blackboard.get("evidence.gaps", []) or []
            gaps.append({
                "agent": stage.id,
                "need": ("retained observation of the designated source(s) "
                         f"{self._locators[:2] or ''} sufficient to ground the deliverable's "
                         "load-bearing claims — retained payload files, not prose reports"),
                "attempted": "review demanded evidence; the courier retained nothing "
                             "(its report was narration)"})
            self.blackboard.put("evidence.gaps", gaps)
            self.tracer.event("review.evidence_gap", stage.id, status="info",
                              note="unmet evidence demand escalated to the adaptive loop")
        self._write_review_record(
            stage.id, record,
            f"{verdict} — rounds exhausted; the final round's critique was NOT applied "
            "(a repair without a budgeted re-verification would ship unreviewed work)")
        return (draft + f"\n\n[engine note] review flagged unresolved issues after "
                        f"{stage.rounds} round(s); see outputs/{stage.id}.r*.md for the critique.")

    def _write_review_record(self, stage_id: str, record: list[dict], final: str) -> None:
        """The review stage's aggregate output is the REVIEWER'S verdict record —
        never the revised draft's self-description (observed: outputs/review.md
        claimed 'all defects resolved' while the final structured verdict was
        'revise'; structured state and narration must not disagree)."""
        agg = Path(config.RUNS_DIR) / self.session / "outputs" / f"{stage_id}.md"
        agg.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# {stage_id} [review] — verdict record (authoritative)", "",
                 f"**FINAL: {final}**", ""]
        for r in record:
            lines += [f"## round {r['round']} — VERDICT: {r['verdict']}", ""]
            crits = r.get("criteria") or []
            if crits:
                lines += ["Per-criterion state:", ""]
                lines += [f"- [{c['status']}] {c['criterion']}"
                          + (f" — {c['evidence']}" if c.get("evidence") else "")
                          for c in crits]
                lines.append("")
            claims = r.get("claims") or []
            if claims:
                lines += ["Claim support (typed; checked against target lineage):", ""]
                lines += [f"- [{c['support']}] {c['claim']}"
                          + (f" — {c['evidence']}" if c.get("evidence") else "")
                          for c in claims]
                lines.append("")
            lines += [r.get("critique") or r.get("error") or "", ""]
        agg.write_text("\n".join(lines))

    def _digest(self, pending: Optional[str] = None,
                just_completed: Optional[tuple] = None) -> str:
        """Compact run-state summary for the supervisor: what a person glancing
        at the situation would want to know before judging it."""
        objective, _history, workspace = self._run_ctx
        lines = [f"OBJECTIVE (the user's own words): {objective}", "", "PLAN:"]
        for s in self._stages_by_id.values():
            status = "done" if s.id in self._outputs else "not yet run"
            lines.append(f"- {s.id} [{s.pattern}] ({status}): {s.goal[:140]}")
        if self._criteria:
            lines.append("\nSUCCESS CRITERIA (from the objective — the deliverable must meet these):")
            lines += [f"- {c}" for c in self._criteria]
        if self._review_verdicts:
            lines.append(
                "\nREVIEW VERDICTS so far (AUTHORITATIVE, in round order): "
                + ", ".join(self._review_verdicts)
                + " — never describe the work as verified/approved/grounded unless "
                  "this says approved; stage prose claiming success is not state.")
        if self._failures:
            lines.append("\nMETHOD FAILURES (controller state — each was converted to a "
                         "residual requirement; a failed method is not evidence):")
            lines += [f"- stage '{f.stage_id}' [{f.phase}/{f.failure_class}]: {f.detail[:160]}"
                      for f in self._failures[-3:]]
        if self._outputs:
            lines.append("")
            lines.append("COMPLETED STAGE OUTPUTS (heads — already streamed to the user's terminal):")
            for k, v in list(self._outputs.items())[-4:]:
                lines.append(f"[{k}]: {(v or '')[:400]}")
        if just_completed is not None:
            sid, out = just_completed
            lines.append(f"\nJUST COMPLETED — stage '{sid}', fuller output:\n{(out or '')[:1200]}")
            # Judge the real artifact, not the agent's description of it.
            observed = _observed_artifacts(workspace, limit=800)
            if observed:
                lines.append(f"\nOBSERVED ARTIFACT FILES this run produced (verify claims against "
                             f"THESE, not the narration above):\n{observed[:2500]}")
        if pending:
            lines.append(f"\nPENDING USER INTERACTION (about to happen in the terminal): {pending}")
        return "\n".join(lines)

    async def _consult_interaction(self, pending: str) -> None:
        """Executive check before asking the user for anything: are they
        equipped to answer? If not, show them what they need first."""
        if self.supervisor is None:
            return
        d = await self.supervisor.consult("pre_interaction", self._digest(pending=pending),
                                          allowed=("proceed", "inform_user"))
        if d.action == "inform_user" and self.broker is not None:
            self.broker.inform(d.message)

    def _reader_dirs(self) -> tuple:
        """Workspace dirs whose files count as ORIGINAL SOURCE EVIDENCE —
        identified by the capability's declared output role (`emits ==
        "observation"`), never by name and never by what it consumes:
        `accepts` only says a tool reads files, and validators, converters and
        summarizers read files too — their output is derived, not source. A
        registry predating output-role typing falls back to the accepts
        heuristic until its capabilities are re-validated under the current
        contract (which forces the declaration)."""
        if self.registry is None:
            return ()
        try:
            caps = self.registry.all()
            obs = [c.name for c in caps if c.emits == "observation"]
            if obs:
                return tuple(obs)
            # No declared perception layer. An EMPTY evidence surface is the
            # worst state (review goes blind — observed when an extractor
            # misdeclared itself 'derived'), so fall back to file-consuming
            # capabilities whose output still originates from the user's
            # inputs; validators and artifact producers stay excluded.
            return tuple(c.name for c in caps
                         if c.accepts and c.emits not in ("validation", "artifact"))
        except Exception:  # noqa: BLE001
            return ()

    def _evidence_capabilities(self, draft_stage: Optional[StageSpec]) -> list[str]:
        """Tools an evidence courier may use: whatever the draft's own evidence
        stages used (e.g. the map workers' fetch tool)."""
        caps: list[str] = []
        if draft_stage is not None:
            for dep in draft_stage.depends_on:
                st = self._stages_by_id.get(dep)
                if st is not None:
                    caps += [c for c in st.capabilities if c not in caps]
        return caps

    # ------------------------------------------------------------------ #
    # sessions
    # ------------------------------------------------------------------ #
    async def _research(self, topic: str, why: str = "", specifics: str = "") -> str:
        """Delegated research (the mediated request→delegate→return pattern, like
        ask_upstream but with tools): run a bounded research agent equipped with
        web_search to investigate a topic an agent said it lacks knowledge of,
        and return a concise findings brief. Shared per-run budget + a topic
        cache (dedup) keep it bounded and traced.

        Honesty guardrail: the brief is labeled METHOD KNOWLEDGE / general web
        testimony. It informs HOW to proceed; it is NOT source evidence about the
        run's designated target and the requester is told so explicitly. (The
        web_search payloads it retains are proxy-tier in the ledger by
        construction, never target-tier, so this cannot launder into grounding.)"""
        objective, history, workspace = self._run_ctx
        key = " ".join(str(topic or "").lower().split())[:200]
        if key in self._research_cache:
            self.tracer.event("research.cache_hit", "research", status="info", topic=key[:120])
            return self._research_cache[key]
        if self._research_used >= config.RESEARCH_MAX_REQUESTS:
            return ("[research] budget exhausted for this run "
                    f"({config.RESEARCH_MAX_REQUESTS} requests). Proceed with what you know, "
                    "or report a blocker if you genuinely cannot continue without it.")
        self._research_used += 1
        n = self._research_used
        spec = AgentSpec(
            id=f"research.{n}", role="Research agent",
            goal=("Investigate the requested topic and return a CONCISE, actionable "
                  "findings brief: the key facts, the concrete how-to or fix, your "
                  "CONFIDENCE in each finding, and the source URLs. Prefer AUTHORITATIVE "
                  "sources: when search snippets are thin or you need exact API/behavior "
                  "details, FETCH and read the actual documentation/release-notes page "
                  "rather than trusting index excerpts. If sources disagree or are thin, "
                  "say so, and state what quick check would verify a brittle claim before "
                  "anyone builds on it. Do not pad."),
            capabilities=["web_search", "fetch_page_text"])
        ctx = (f"A teammate needs to learn this to proceed.\nTOPIC: {topic}\n"
               + (f"WHY (what they're stuck on): {why}\n" if why else "")
               + (f"SPECIFICS (error text / versions / constraints): {specifics}\n" if specifics else "")
               + "\nResearch it now and return the brief. Cite the URLs you used and mark "
                 "each key finding with confidence (confirmed-from-authoritative-page / "
                 "snippet-only / inferred).")
        # A page fetcher is authored on first use; give it a clean
        # instance-neutral contract instead of the stage-goal fallback.
        self._cap_needs.setdefault("fetch_page_text", {
            "contract": ("Fetch the page or document at a provided URL and extract its "
                         "readable text content. Inputs: the URL (and optionally a byte/char "
                         "limit). Output: the extracted text, retained as a payload file, "
                         "with source_refs naming the URL actually fetched, honest "
                         "coverage/observation_state reporting, and unresolved needs for "
                         "content that could not be rendered or extracted."),
            "example_input": {}})
        try:
            caps = await self._acquire_caps(spec)
        except Exception as e:  # noqa: BLE001 — research is best-effort, never fatal
            self.tracer.event("research.failed", spec.id, status="failed",
                              error=f"{type(e).__name__}: {str(e)[:160]}")
            return ("[research] could not be carried out (the research tool was unavailable). "
                    "Proceed with what you know or report a blocker.")
        from .observability import activity

        # Research runs INSIDE the requester's tool call — its session is
        # silent throughout and must not be stall-killed for waiting
        # (observed: a watchdog killed the requesting developer mid-research
        # and the finished research was discarded with the cascade).
        with activity.working(), self.tracer.span("research", spec.id, topic=key[:120]):
            findings = await self._run_agent_with_reflection(
                spec, caps, objective, {}, history, workspace, task_context=ctx,
                model=config.model_for("research"), team=False,
                max_turns=config.RESEARCH_AGENT_MAX_TURNS)
        brief = ("[research findings — METHOD KNOWLEDGE / general web testimony, NOT source "
                 "evidence about your task's designated target; use it to choose your "
                 "approach, never present it as an observation of the target. Before an "
                 "EXPENSIVE retry (a rebuild, a long run), verify any snippet-only claim "
                 "the brief flags]\n\n"
                 + (findings or "(no findings returned)"))
        self._research_cache[key] = brief
        return brief

    async def _answer_from_stage(self, stage_id: str, question: str) -> str:
        """Run a short, tool-less follow-up session of a completed stage's agent
        so a teammate's question gets answered by 'the person who did the work'."""
        objective, history, workspace = self._run_ctx
        st = self._stages_by_id.get(stage_id)
        prev = (self._outputs.get(stage_id) or "")[:40000]
        self._questions_answered += 1
        spec = AgentSpec(id=f"{stage_id}.q{self._questions_answered}",
                         role=(st.role if st else stage_id),
                         goal="Answer a teammate's question about your earlier work, "
                              "concisely and factually.")
        ctx = ("Your earlier output:\n---\n" + prev + "\n---\n\n"
               "Teammate's question: " + question +
               "\nAnswer strictly from your output and context; if you do not know, say so.")
        from .observability import activity

        # Same delegated-work window as research: the ASKER's session is
        # silent while this answer session runs.
        with activity.working():
            return await self._run_agent_with_reflection(
                spec, {}, objective, {}, history, workspace, task_context=ctx, team=False)

    def _evidence_server(self, workspace: Path):
        """In-process MCP server giving verification-role agents deterministic,
        engine-side claim lookup over the original evidence — cheap perception
        (look it up) instead of social epistemology (ask a colleague's memory).
        The searched surface is recomputed PER CALL so evidence a courier
        retains mid-review is immediately findable, and it includes
        target-lineage payloads whatever role their producing tools declared."""
        from claude_agent_sdk import create_sdk_mcp_server, tool

        reader_dirs = self._reader_dirs()

        @tool("locate_evidence",
              "Search the run's ORIGINAL evidence (user inputs + raw extractions + "
              "target-lineage payloads) for an "
              "exact number/phrase/quote. Returns each match with file, line, and context, "
              "or an authoritative NOT FOUND. Use it to settle any disputed claim by "
              "direct lookup before calling it unverified or fabricated.",
              {"type": "object", "properties": {"query": {"type": "string"}},
               "required": ["query"]})
        async def locate_evidence(args):
            entries = evidence_mod.ledger(workspace, reader_dirs, self._locators)
            lineage_paths = [e.file for e in evidence_mod.target_lineage(entries)]
            result = _locate_evidence(workspace, reader_dirs, args.get("query", ""),
                                      extra_paths=lineage_paths)
            self.tracer.event("review.lookup", "evidence", status="info",
                              query=str(args.get("query", ""))[:120],
                              found=result.startswith("FOUND"))
            return {"content": [{"type": "text", "text": result}]}

        # File viewing (incl. images-as-images) is the general read_file tool
        # every stage agent already has; review needs no separate copy.
        return create_sdk_mcp_server(name="evidence", version="1.0.0",
                                     tools=[locate_evidence])

    async def _run_agent_with_reflection(self, spec, caps, objective, upstream, history,
                                         workspace, task_context: Optional[str] = None,
                                         model: Optional[str] = None, team: bool = True,
                                         max_turns: Optional[int] = None,
                                         extra_servers: Optional[dict] = None,
                                         extra_tools: Optional[list] = None) -> str:
        """Run an agent; if a tool it relied on degrades at runtime, reflect —
        repair that tool and re-run the agent — bounded, then best-effort."""
        from .capabilities.health import HealthMonitor
        from .observability import trace_query

        if self._sessions >= config.RUN_MAX_AGENT_SESSIONS:
            self.tracer.event("run.session_cap", spec.id, status="failed",
                              cap=config.RUN_MAX_AGENT_SESSIONS)
            return (f"(not run: this run reached its cap of "
                    f"{config.RUN_MAX_AGENT_SESSIONS} agent sessions)")
        self._sessions += 1

        runner = self._session_runner or trace_query
        completed = list(getattr(self, "_outputs", {}) or {})
        channel = TeamChannel(agent_id=spec.id, completed_stages=completed,
                              ask_fn=self._answer_from_stage, blackboard=self.blackboard,
                              tracer=self.tracer, research_fn=self._research) if team else None
        allowed = [f"mcp__caps__{name}" for name in caps]
        if channel is not None:
            allowed = allowed + TEAM_TOOLS
        # Multimodal perception is the agent's own ability: every real stage
        # agent can open retained files (images shown AS images) and can
        # ENUMERATE what the run holds — guessed paths and truncated error
        # listings were observed producing false "file is absent" beliefs.
        if channel is not None and workspace is not None:
            allowed = allowed + ["mcp__workspace__read_file",
                                 "mcp__workspace__list_files"]
        if extra_tools:
            allowed = allowed + list(extra_tools)
        session_secrets = getattr(self.broker, "secrets", {}) or {}
        ready = [c.name for c in caps.values()
                 if c.requires_credential and session_secrets.get(c.requires_credential)]
        system_prompt = factory.agent_system_prompt(
            spec, allowed, team_stages=(completed if channel is not None else None),
            ready_credentials=ready or None)
        # The run's file state is an engine-known FACT — hand it to the agent
        # instead of letting it guess paths (a worker was observed declaring
        # its workspace empty while every needed file sat under a seeded
        # directory it had no way to discover).
        inventory = None
        if channel is not None and workspace is not None:
            try:
                files = _workspace_files(Path(workspace))
                if files:
                    inventory = (
                        f"{len(files)} file(s), by directory (counts with "
                        "first … last names):\n"
                        + "\n".join(_workspace_rollup(files))
                        + "\n(Snapshot from session start — files produced later are "
                          "not listed. list_files enumerates the live state; read_file "
                          "opens any path. Never infer a file's absence from this "
                          "summary or from a truncated listing.)")
            except OSError:
                inventory = None
        repairs = 0
        out = ""
        while True:
            monitor = HealthMonitor()
            servers: dict = {}
            if caps:
                servers["caps"] = build_caps_server(
                    list(caps.values()), manager=self.manager,
                    workspace=workspace, monitor=monitor, tracer=self.tracer,
                    secrets=getattr(self.broker, "secrets", None))
            if channel is not None:
                servers["team"] = build_team_server(channel)
            if channel is not None and workspace is not None:
                servers["workspace"] = self._workspace_server(workspace)
            if extra_servers:
                servers.update(extra_servers)
            options = factory.locked_session(
                system_prompt=system_prompt,
                mcp_servers=servers,
                allowed_tools=allowed,
                model=(model if model is not None else self.model),
                max_turns=max_turns or config.AGENT_MAX_TURNS,
            )
            out = await runner(
                self.tracer, spec.id,
                factory.agent_user_prompt(objective, spec, upstream, history=history,
                                          task_context=task_context,
                                          workspace_inventory=inventory),
                options,
            )

            # Carry verbatim-conduit evidence to run level (quarantine happens
            # at delivery, long after this attempt's monitor is gone).
            for name, v in monitor.verbatim.items():
                self._verbatim_caps[name] = self._verbatim_caps.get(name, True) and v

            # Reflect: did any tool this agent used degrade in practice?
            sick = [(name, reason) for name in caps if (reason := monitor.unhealthy(name))]
            if not sick or repairs >= config.CAPABILITY_MAX_REPAIRS_PER_RUN:
                break
            repaired_any = False
            for name, reason in sick:
                self.tracer.event("capability.unhealthy", name, status="failed",
                                  reason=reason, stats=monitor.summary(name))
                try:
                    with self.tracer.span("capability.repair", name, reason=reason):
                        # Decision-aware routing (P1-D): a seeded fix with the
                        # live failure in hand is bounded mechanics — fast tier.
                        new_cap = await acquire_repair(
                            caps[name], reason, manager=self.manager, registry=self.registry,
                            broker=self.broker,
                            model=config.model_for("repair") or self.model,
                            tracer=self.tracer, research_fn=self._research,
                            workspace=workspace,
                        )
                    caps[name] = new_cap
                    repaired_any = True
                except Exception as e:  # noqa: BLE001
                    self.tracer.event("capability.repair", name, status="failed", error=str(e)[:300])
            if not repaired_any:
                break
            repairs += 1
            self.tracer.event("agent.rerun", spec.id, status="info", after_repair=repairs)

        _persist_agent(config.RUNS_DIR, self.session, spec, system_prompt, out)
        return out

    async def _maybe_repair_and_rerun(self, stage: StageSpec, spec: AgentSpec,
                                      caps: dict, out: str, objective: str,
                                      upstream: dict, history: Optional[str],
                                      workspace: Path,
                                      task_context: Optional[str]) -> str:
        """Quality-driven tool evolution: an agent that REPORTED one of its
        tools defective gets the tool repaired and ONE re-run with it. Closes
        the dead end observed twice: a broken/inexpressive tool ended as a
        blocker note and a silently degraded deliverable (charts dropped to
        tables) while `repair_capability` sat unused — failure-driven repair
        existed, agent-reported repair did not. Bounded per run."""
        if self.blackboard is None or self.manager is None or self.registry is None:
            return out
        defects = self.blackboard.get("tool.defects", []) or []
        mine = [d for d in defects
                if str(d.get("agent", "")).startswith(stage.id)
                and d.get("tool") in caps and not d.get("handled")]
        if not mine:
            return out
        for d in mine:
            d["handled"] = True
        self.blackboard.put("tool.defects", defects)
        if self._tool_repairs >= config.TOOL_DEFECT_REPAIRS_PER_RUN:
            self.tracer.event("capability.defect_unrepaired", stage.id, status="failed",
                              tool=mine[-1].get("tool", ""),
                              note="tool-defect repair budget exhausted")
            return out
        d = mine[-1]
        name = str(d.get("tool"))
        self._tool_repairs += 1
        reason = (f"agent-reported defect: {d.get('error', '')[:400]}"
                  + (f" (tried: {d.get('tried', '')[:200]})" if d.get("tried") else ""))
        try:
            with self.tracer.span("capability.repair", name,
                                  reason="agent-reported defect"):
                new_cap = await acquire_repair(
                    caps[name], reason, manager=self.manager, registry=self.registry,
                    broker=self.broker, model=config.model_for("repair") or self.model,
                    tracer=self.tracer, research_fn=self._research, workspace=workspace)
            caps[name] = new_cap
        except Exception as e:  # noqa: BLE001 — a failed repair leaves the honest fallback
            self.tracer.event("capability.repair_failed", name, status="failed",
                              error=f"{type(e).__name__}: {str(e)[:200]}")
            return out
        self.tracer.event("stage.rerun_after_repair", stage.id, status="info", tool=name)
        note = ((task_context or "")
                + f"\n\nENGINE NOTE: your tool '{name}' was REPAIRED in response to your "
                  f"defect report ({d.get('error', '')[:200]}). Redo the task now USING "
                  "the repaired tool — do not keep the degraded workaround. Your previous "
                  "attempt, for reference:\n---\n" + out[:4000] + "\n---")
        spec2 = AgentSpec(id=f"{stage.id}.retool", role=spec.role, goal=spec.goal,
                          capabilities=spec.capabilities, depends_on=spec.depends_on)
        return await self._run_agent_with_reflection(
            spec2, caps, objective, upstream, history, workspace, task_context=note)

    async def _acquire_caps_best_effort(self, spec: AgentSpec) -> tuple[dict, str]:
        """Acquire what can be acquired, degrading per-capability instead of
        failing the caller: verification-role sessions must review with the
        tools that exist rather than die on the one that could not be built.
        Returns (caps, note) — the note names what is missing and why."""
        caps: dict[str, Capability] = {}
        missing: list[str] = []
        for need in spec.capabilities:
            single = AgentSpec(id=spec.id, role=spec.role, goal=spec.goal,
                               capabilities=[need], depends_on=spec.depends_on)
            try:
                caps.update(await self._acquire_caps(single))
            except Exception as e:  # noqa: BLE001 — degrade per-capability, never die
                fc = getattr(e, "failure_class", type(e).__name__)
                self.tracer.event("review.capability_degraded", need, status="failed",
                                  agent=spec.id, error=f"{fc}: {str(e)[:200]}")
                missing.append(f"'{need}' ({fc}: {str(e)[:120]})")
        note = ""
        if missing:
            note = ("NOTE: these capabilities could NOT be built for this session — "
                    + "; ".join(missing) + ". Work with the tools you have "
                    "(locate_evidence, read_file); if something load-bearing cannot be "
                    "adjudicated without the missing tool, say so explicitly in your "
                    "verdict rather than guessing.")
        return caps, note

    def _contract_mismatch(self, need: str, cap) -> str:
        """Deterministic reuse guard: every key of the stage's example_input
        must be accepted by the cached tool's declared input schema, or the
        tool serves a DIFFERENT interface than this contract needs, however
        well its generalized description matches. Returns the mismatch reason,
        or '' when the interface fits. `files` is exempt (file references are
        mounted by the engine, and extension compatibility is checked at call
        time by incompatible_refs); a schema-less tool constrains nothing."""
        need_spec = self._cap_needs.get(need) or {}
        example = need_spec.get("example_input") or {}
        if not isinstance(example, dict) or not example:
            return ""
        props = (cap.input_schema or {}).get("properties") or {}
        if not props:
            return ""
        missing = sorted(k for k in example if k not in props and k != "files")
        if not missing:
            return ""
        return (f"stage example_input keys {missing} are not in the cached tool's "
                f"declared schema (accepts: {sorted(props)}) — its interface serves "
                "a different contract; re-authoring under this stage's contract")

    async def _acquire_caps(self, spec: AgentSpec) -> dict[str, Capability]:
        caps: dict[str, Capability] = {}
        for need in spec.capabilities:
            with self.tracer.span("resolve", need, agent=spec.id):
                decision = self.resolver.resolve(need)
                if decision.kind == REUSE and decision.capability is not None:
                    # INTERFACE CONFORMANCE: description-level matching cannot
                    # see parameter schemas — a cached writer whose schema
                    # demanded its birth task's bespoke record fields was
                    # reused for plain text-and-filename writes and served an
                    # alien interface for a whole run. The stage's example
                    # input must be expressible in the tool's declared schema,
                    # checked deterministically before any container spins up.
                    mismatch = self._contract_mismatch(need, decision.capability)
                    if mismatch:
                        self.tracer.event("capability.contract_mismatch",
                                          decision.capability.name, status="failed",
                                          need=need, reason=mismatch[:300])
                        decision = Decision(kind=CREATE, capability_need=need)
                if decision.kind == REUSE and decision.capability is not None:
                    cap = decision.capability
                    # Immune system (#9): memory promoted under an older engine
                    # OR an older authoring contract must pass the CURRENT
                    # battery before it is trusted — the epoch fingerprints the
                    # contract, so contract changes invalidate without a release.
                    from .acquisition import revalidate_capability, validation_epoch
                    epoch = validation_epoch()
                    if (cap.validated_engine != epoch
                            and self.manager is not None and self.registry is not None):
                        with self.tracer.span("capability.revalidate", cap.name,
                                              stamped=cap.validated_engine or "never"):
                            ok, reason = await asyncio.to_thread(
                                revalidate_capability, cap, self.manager, self.registry)
                        if ok:
                            # Legacy migration rides on revalidation: promoted
                            # descriptions predating consolidation hygiene may
                            # still carry task residue (originating URL, output
                            # policy) — normalize before re-trusting, or the
                            # residue keeps anchoring every future plan.
                            clean = generalize_description(cap.description)
                            if clean != cap.description:
                                cap.description = clean
                                self.registry.add(cap)
                                self.tracer.event("capability.normalized", cap.name,
                                                  status="info", description=clean[:200])
                            self.registry.stamp_validated(cap.name, epoch)
                        else:
                            # Normalize the stored description on the FAILURE
                            # path too — quarantine used to preserve the
                            # task-shaped text, which kept anchoring plans.
                            clean = generalize_description(cap.description)
                            if clean != cap.description:
                                cap.description = clean
                                self.registry.add(cap)
                            self.registry.quarantine(cap.name, reason)
                            self.tracer.event("capability.quarantined", cap.name,
                                              status="failed", reason=reason[:300])
                            # Re-author under the current contract instead.
                            decision = Decision(kind=CREATE, capability_need=need)
                if decision.kind == REUSE and decision.capability is not None:
                    cap = decision.capability
                    # A reused credentialed tool still needs its secret THIS session.
                    if cap.requires_credential and cap.requires_credential not in (
                            getattr(self.broker, "secrets", {}) or {}):
                        await self._consult_interaction(
                            f"approval + hidden prompt for credential {cap.requires_credential}, "
                            f"needed to reuse the tool '{cap.name}' ({cap.description[:100]}); "
                            f"stored retrieval instructions: "
                            f"{'yes' if cap.credential_help else 'NO — none on file'}")
                        granted = self.broker is not None and self.broker.request(
                            action=f"reuse capability '{cap.name}', which needs {cap.requires_credential}",
                            reason="the tool requires a credential; its value is held per session only",
                            requirements=[cap.requires_credential],
                        ) and self.broker.collect_secret(
                            cap.requires_credential, purpose=f"capability '{cap.name}'",
                            help_text=cap.credential_help)
                        if not granted:
                            self.tracer.event("capability.unavailable", cap.name, status="denied",
                                              needs=cap.requires_credential, agent=spec.id)
                            continue  # the agent works without it and reports honestly
                    self.tracer.event("decision", need, status="info",
                                      choice="reuse", capability=cap.name, agent=spec.id)
                    caps[cap.name] = cap
                elif decision.kind == CREATE and decision.capability_need:
                    if need in self._suppressed_caps:
                        # Scoped negative memory: this method already failed this
                        # run; blind re-authoring is exactly the repaired-forever
                        # loop. A NEW contract from the planner lifts suppression.
                        raise AcquisitionError(
                            f"method '{need}' already failed this run and is suppressed "
                            "for this recovery cycle (no new contract/rationale given)",
                            need=need, failure_class="suppressed")
                    builder = make_builder_agent(self.model, self.tracer) if self.use_builder else None
                    need_spec = self._cap_needs.get(need) or {}
                    # P0-B: the developer sees an INSTANCE-NEUTRAL contract — the
                    # planner's compiled capability need when present, else the
                    # stage goal scrubbed of task residue — while the concrete
                    # instance values ride separately, for verification only.
                    # Passing the whole raw stage goal was observed compiling a
                    # URL and a ten-sentence output policy into three
                    # generations of an unusable observer-and-summarizer.
                    description = generalize_description(
                        need_spec.get("contract") or f"{spec.role}: {spec.goal}")
                    cap = await acquire(
                        need,
                        manager=self.manager,
                        registry=self.registry,
                        broker=self.broker,
                        builder=builder,
                        description=description,
                        model=self.model,
                        tracer=self.tracer,
                        pre_interaction=self._consult_interaction,
                        example_input=need_spec.get("example_input") or None,
                        research_fn=self._research,
                        workspace=(self._run_ctx[2] if getattr(self, "_run_ctx", None)
                                   else None),
                        failure_log=getattr(self, "_acq_failures", None),
                    )
                    caps[cap.name] = cap
        return caps
