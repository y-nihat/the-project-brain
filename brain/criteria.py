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

# exactly 3 sections | no more than ten sentences | at most 40 words | under 5 bullets
_CONSTRAINT = re.compile(
    r"\b(?P<op>exactly|precisely|no more than|at most|maximum(?: of)?|up to|not exceed(?:ing)?|"
    r"under|fewer than|less than|at least|minimum(?: of)?|no fewer than)\s+"
    r"(?P<num>\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
    r"(?P<unit>sentence|paragraph|word|section|heading|bullet|item|line|slide|page)s?\b",
    re.IGNORECASE)
# "a single paragraph" / "one paragraph" with no explicit operator = exactly 1.
_IMPLICIT_ONE = re.compile(
    r"\b(?P<num>a single|single|one)\s+(?P<unit>sentence|paragraph|section|slide|page)\b",
    re.IGNORECASE)

# Units the engine can count unambiguously — a measured violation of these is
# authoritative. Sentences stay advisory (segmentation is heuristic).
_GATING_UNITS = ("paragraph", "word", "section", "heading", "bullet", "item",
                 "line", "slide")


@dataclass
class CountCheck:
    criterion: str
    unit: str
    op: str          # "max" | "min" | "exact"
    bound: int
    measured: int
    ok: bool
    gating: bool     # measured unambiguously enough to void an approval


def _count(unit: str, text: str) -> int:
    t = (text or "").strip()
    if not t:
        return 0
    if unit == "paragraph":
        return len([b for b in re.split(r"\n\s*\n", t) if b.strip()])
    if unit == "word":
        return len(t.split())
    if unit in ("section", "heading"):
        return len(re.findall(r"^#{1,6}\s+\S|^[^\n]+\n[=-]{3,}\s*$", t, re.MULTILINE))
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
            measured = _count(unit, payload)
            ok = (measured == bound if op == "exact"
                  else measured >= bound if op == "min"
                  else measured <= bound)
            checks.append(CountCheck(criterion=c, unit=unit, op=op, bound=bound,
                                     measured=measured, ok=ok,
                                     gating=unit in _GATING_UNITS))
    return checks


def violations(checks: list[CountCheck]) -> list[CountCheck]:
    """The measured, authoritative failures (gating units only)."""
    return [c for c in checks if not c.ok and c.gating]


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


def describe(checks: list[CountCheck]) -> str:
    if not checks:
        return ""
    lines = ["ENGINE-MEASURED FORMAT FACTS (counted against the deliverable "
             "PAYLOAD — these measurements are authoritative for gating units; "
             "sentence counts are advisory):"]
    for c in checks:
        verdict = "OK" if c.ok else ("VIOLATED" if c.gating else "over/under (advisory)")
        lines.append(f"- {c.op} {c.bound} {c.unit}(s) required; measured {c.measured} "
                     f"-> {verdict}  [{c.criterion[:90]}]")
    return "\n".join(lines)
