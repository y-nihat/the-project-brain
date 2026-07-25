"""Deterministic checks for COUNT-SHAPED success criteria.

The V060 failure this closes: the objective demanded exactly one paragraph;
the saved deliverable had six content blocks plus a change log; the reviewer
counted only the embedded paragraph and stamped criteria=approved. Countable
constraints (paragraphs, words, sections, items) are not judgment calls — the
engine measures them against the DELIVERABLE PAYLOAD and a measured violation
is authoritative over any verdict prose.

Scope discipline: only constraints that parse cleanly (an explicit bound + a
countable unit) are checked, and only units the engine can count unambiguously
may VOID an approval. Sentence counts are reported as measured facts for the
reviewer but never gate on their own — sentence segmentation is heuristic.
Everything here is pure and I/O-free; nothing is task- or domain-specific.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                 "twelve": 12, "a single": 1, "single": 1, "an": 1, "a": 1}

_UNIT = r"(?P<unit>sentence|paragraph|word|section|heading|bullet|item|line|slide|page)s?\b"
# A bounded window of MODIFIERS between the bound and the unit. People write
# "exactly 3 DISTINCT pages" and "exactly three FULL-VIEWPORT sections"; the
# unit had to follow the number immediately, so a single adjective dropped the
# criterion out of deterministic measurement and back into model judgment —
# silently, because an unparsed constraint is indistinguishable from an
# unbounded one. The window is deliberately short: two words admit the
# adjectives that occur in practice while keeping a number and a far-away noun
# from being read as a constraint ("exactly 2 of the 10 questions … paragraph").
_MODIFIERS = r"(?:[\w-]+\s+){0,2}"

# exactly 3 sections | no more than ten sentences | at most 40 words | under 5 bullets
_CONSTRAINT = re.compile(
    r"\b(?P<op>exactly|precisely|no more than|at most|maximum(?: of)?|up to|not exceed(?:ing)?|"
    r"under|fewer than|less than|at least|minimum(?: of)?|no fewer than)\s+"
    r"(?P<num>\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
    + _MODIFIERS + _UNIT,
    re.IGNORECASE)
# "a single paragraph" / "one paragraph" with no explicit operator = exactly 1.
_IMPLICIT_ONE = re.compile(
    r"\b(?P<num>a single|single|one)\s+" + _MODIFIERS
    + r"(?P<unit>sentence|paragraph|section|slide|page)s?\b",
    re.IGNORECASE)

# Units the engine can count unambiguously — a measured violation of these is
# authoritative. Sentences stay advisory (segmentation is heuristic).
_GATING_UNITS = ("paragraph", "word", "section", "heading", "bullet", "item",
                 "line", "slide")

# THE COUNTING RULE for sections (stated wherever measurements are shown, so
# authors and reviewers know exactly what the engine counts): sections are the
# headings at the BODY's top structural level; a single leading heading that is
# strictly shallower than every other heading is the document TITLE, not a
# section. A load-bearing check with wrong semantics is a false-violation
# generator — a fully compliant deliverable (title + exactly N sections) was
# measured N+1 VIOLATED, and reviewers, bound to trust the measurement over
# their own eyes, burned every bounded round chasing the phantom.
SECTION_RULE = ("sections are counted at the body's top heading level; a single "
                "leading shallower heading is the document title, NOT a section; "
                "deeper subheadings are not sections")

_ATX_HEADING = re.compile(r"^(#{1,6})\s+\S", re.MULTILINE)
_SETEXT_HEADING = re.compile(r"^[^\n]+\n(={3,}|-{3,})\s*$", re.MULTILINE)


def _headings(text: str) -> list[int]:
    """All markdown heading LEVELS in document order (ATX ``#``..``######`` and
    setext ``===``/``---`` underlines, which are levels 1 and 2)."""
    found: list[tuple[int, int]] = []
    for m in _ATX_HEADING.finditer(text):
        found.append((m.start(), len(m.group(1))))
    for m in _SETEXT_HEADING.finditer(text):
        found.append((m.start(), 1 if m.group(1)[0] == "=" else 2))
    return [level for _, level in sorted(found)]


def _section_count(text: str) -> int:
    """Count sections per SECTION_RULE: drop a lone leading heading strictly
    shallower than everything after it (the title), then count headings at the
    shallowest remaining level (subheadings are structure inside a section,
    not additional sections)."""
    levels = _headings(text)
    if not levels:
        return 0
    if len(levels) > 1 and levels[0] < min(levels[1:]):
        levels = levels[1:]
    top = min(levels)
    return sum(1 for lv in levels if lv == top)


@dataclass
class CountCheck:
    criterion: str
    unit: str
    op: str          # "max" | "min" | "exact"
    bound: int
    measured: int
    ok: bool
    gating: bool     # measured unambiguously enough to void an approval
    scope: str = ""  # "" = whole deliverable; "section N" = measured within it


# A count constraint may be SCOPED to a part of the deliverable ("Section 1
# is one paragraph", "each answer under 40 words"). Measuring a scoped bound
# against the WHOLE text is a false-violation generator with authority: an
# approved audit was voided at its final round because "Section 1 - one
# paragraph" was measured as nineteen paragraphs of full document. A scoped
# constraint is measured WITHIN its identifiable part, and demoted to
# advisory when the part cannot be identified deterministically.
_SECTION_SCOPE = re.compile(r"(?i)\bsection\s+(\d{1,2})\b")
_PER_ITEM_SCOPE = re.compile(r"(?i)\b(each|every|per)\s+\w")


def _section_span(text: str, n: int):
    """The Nth body section's CONTENT (1-based, title excluded per
    SECTION_RULE, the section's own heading line excluded — a heading is
    structure, not a countable paragraph/line of the section), or None when
    the document has no such section."""
    marks: list = []   # (heading start, level, content start)
    for m in _ATX_HEADING.finditer(text):
        nl = text.find("\n", m.start())
        marks.append((m.start(), len(m.group(1)),
                      nl + 1 if nl >= 0 else len(text)))
    for m in _SETEXT_HEADING.finditer(text):
        nl = text.find("\n", m.end() - 1)
        marks.append((m.start(), 1 if m.group(1)[0] == "=" else 2,
                      nl + 1 if nl >= 0 else len(text)))
    marks.sort()
    if not marks:
        return None
    levels = [lv for _, lv, _c in marks]
    if len(levels) > 1 and levels[0] < min(levels[1:]):
        marks = marks[1:]
        levels = levels[1:]
    if not marks:
        return None
    top = min(levels)
    tops = [(pos, cstart) for pos, lv, cstart in marks if lv == top]
    if n < 1 or n > len(tops):
        return None
    end = tops[n][0] if n < len(tops) else len(text)
    return text[tops[n - 1][1]:end]


def _count(unit: str, text: str) -> int:
    t = (text or "").strip()
    if not t:
        return 0
    if unit == "paragraph":
        return len([b for b in re.split(r"\n\s*\n", t) if b.strip()])
    if unit == "word":
        return len(t.split())
    if unit == "section":
        return _section_count(t)
    if unit == "heading":
        # "heading" counts EVERY heading (title included) — the explicit unit
        # asks for headings, not document structure.
        return len(_headings(t))
    if unit in ("bullet", "item"):
        return len(re.findall(r"^\s*(?:[-*+•]|\d+[.)])\s+\S", t, re.MULTILINE))
    if unit == "line":
        return len([ln for ln in t.splitlines() if ln.strip()])
    if unit == "slide":
        return max(1, len(re.findall(r"^---\s*$", t, re.MULTILINE)) + 1)
    if unit == "sentence":
        # Heuristic: terminal punctuation not preceded by a known abbreviation
        # or a single capital initial. Advisory only.
        guarded = re.sub(r"\b(?:e\.g|i\.e|etc|vs|dr|mr|mrs|ms|prof|no|fig|st)\.\s",
                         "X ", t, flags=re.IGNORECASE)
        guarded = re.sub(r"\b[A-Z]\.\s", "X ", guarded)
        return len([s for s in re.split(r"[.!?]+(?:\s|$)", guarded) if s.strip()])
    return 0


def _op_kind(op: str) -> str:
    op = op.lower()
    if op in ("exactly", "precisely"):
        return "exact"
    if op.startswith(("at least", "minimum", "no fewer")):
        return "min"
    return "max"


def check_counts(criteria: list[str], payload: str) -> list[CountCheck]:
    """Measure every parseable count constraint against the deliverable
    payload. Returns ALL checks (met and violated) so the reviewer sees the
    engine's measurements instead of re-counting by eye."""
    checks: list[CountCheck] = []
    for c in criteria or []:
        seen: set[tuple] = set()
        for m in list(_CONSTRAINT.finditer(c)) + list(_IMPLICIT_ONE.finditer(c)):
            unit = m.group("unit").lower()
            raw = m.group("num").lower()
            bound = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw, 0)
            op = _op_kind(m.groupdict().get("op") or "exactly")
            key = (unit, op, bound)
            if not bound or key in seen:
                continue
            seen.add(key)
            scope, gating = "", unit in _GATING_UNITS
            target_text = payload
            sm = _SECTION_SCOPE.search(c)
            if sm and unit not in ("section", "heading"):
                span = _section_span(payload, int(sm.group(1)))
                if span is not None:
                    target_text = span
                    scope = f"section {sm.group(1)}"
                else:
                    gating = False
                    scope = f"section {sm.group(1)} (not found - advisory)"
            elif _PER_ITEM_SCOPE.search(c) and unit not in ("section", "heading"):
                # "each/every X under N words" binds per item — a global
                # count cannot adjudicate it; advisory only.
                gating = False
                scope = "per-item (advisory)"
            measured = _count(unit, target_text)
            ok = (measured == bound if op == "exact"
                  else measured >= bound if op == "min"
                  else measured <= bound)
            checks.append(CountCheck(criterion=c, unit=unit, op=op, bound=bound,
                                     measured=measured, ok=ok,
                                     gating=gating, scope=scope))
    return checks


def violations(checks: list[CountCheck]) -> list[CountCheck]:
    """The measured, authoritative failures (gating units only)."""
    return [c for c in checks if not c.ok and c.gating]


# A criterion that STATES a bound: an operator word plus a number. If one of
# these yields no check, the bound is real but the engine cannot measure it.
_BOUNDED_SHAPE = re.compile(
    r"\b(?:exactly|precisely|no more than|at most|maximum|up to|not exceed|under|"
    r"fewer than|less than|at least|minimum|no fewer than)\s+"
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b",
    re.IGNORECASE)


def unmeasurable_counts(criteria: list[str]) -> list[str]:
    """Criteria that state a numeric bound the engine cannot measure.

    Coverage is the honest problem here, not correctness: across the recorded
    corpus only ~10% of compiled criteria parse into a deterministic check,
    and the rest are almost all bounds on units the measurement grammar does
    not know ("exactly 8 questions", "at least one chart image"). Those rest
    entirely on model judgment — which is fine, and is exactly what the
    reviewer is for — but it was INVISIBLE: an unparsed constraint looked
    identical to no constraint at all.

    Naming them lets the reviewer know which bounds it alone is accountable
    for, and shows an operator where extending the grammar would pay.
    """
    out: list[str] = []
    for c in criteria or []:
        if _BOUNDED_SHAPE.search(c or "") and not check_counts([c], ""):
            out.append(c)
    return out


def artifact_page_check(criteria: list[str], page_count: int) -> list[CountCheck]:
    """Measure slide/page count constraints against the RENDERED artifact
    (pages actually produced by rendering it), not against the text payload —
    an 11-page artifact shipped against an explicit 10-page requirement
    because the count was only ever measured on prose. Gating: a render is an
    unambiguous count."""
    checks: list[CountCheck] = []
    for crit in criteria or []:
        for m in list(_CONSTRAINT.finditer(crit)) + list(_IMPLICIT_ONE.finditer(crit)):
            unit = m.group("unit").lower()
            if unit not in ("slide", "page"):
                continue
            raw = m.group("num").lower()
            bound = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw, 0)
            if not bound:
                continue
            op = _op_kind(m.groupdict().get("op") or "exactly")
            ok = (page_count == bound if op == "exact"
                  else page_count >= bound if op == "min"
                  else page_count <= bound)
            checks.append(CountCheck(criterion=crit, unit=unit, op=op, bound=bound,
                                     measured=page_count, ok=ok, gating=True))
    return checks


def html_countable_text(html: str) -> str:
    """Deterministic HTML -> countable-text projection, so count criteria can
    be measured against an HTML artifact of record instead of a payload that
    merely points at it (a compliant dashboard was measured '0 sections'
    because only its one-line pointer payload was ever counted). Headings map
    to ATX (#..######), list items to bullets, block ends to paragraph breaks;
    script/style/comments are dropped. Pure text projection — no parser deps."""
    t = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", html or "")
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = re.sub(r"(?is)<h([1-6])[^>]*>", lambda m: "\n\n" + "#" * int(m.group(1)) + " ", t)
    t = re.sub(r"(?is)</h[1-6]\s*>", "\n\n", t)
    t = re.sub(r"(?is)<li[^>]*>", "\n- ", t)
    t = re.sub(r"(?is)<(?:br|hr)\s*/?\s*>", "\n", t)
    t = re.sub(r"(?is)</(?:p|div|section|article|table|tr|ul|ol|blockquote|"
               r"figure|header|footer|main|aside|nav)\s*>", "\n\n", t)
    t = re.sub(r"(?s)<[^>]{0,600}>", " ", t)
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        t = t.replace(ent, ch)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" ?\n ?", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def describe(checks: list[CountCheck], target: str = "the deliverable PAYLOAD") -> str:
    if not checks:
        return ""
    lines = [f"ENGINE-MEASURED FORMAT FACTS (counted against {target} — these "
             "measurements are authoritative for gating units; sentence counts "
             "are advisory):"]
    for c in checks:
        verdict = "OK" if c.ok else ("VIOLATED" if c.gating else "over/under (advisory)")
        where = f" within {c.scope}" if c.scope else ""
        lines.append(f"- {c.op} {c.bound} {c.unit}(s) required{where}; measured "
                     f"{c.measured} -> {verdict}  [{c.criterion[:90]}]")
    if any(c.unit in ("section", "heading") for c in checks):
        lines.append(f"(Counting rule: {SECTION_RULE}.)")
    return "\n".join(lines)
