"""Pure workflow mechanics: manifests, sharding, stage normalization, ordering.

The staged-workflow model ("constrain locally, aggregate globally"):

  * a stage that feeds a ``map`` ends its output with a fenced **manifest** — a
    JSON array of work items — so fan-out width is decided from what was
    actually found, not guessed at plan time;
  * :func:`shard` splits the manifest into per-worker groups, widening shards
    rather than exceeding the worker cap;
  * :func:`normalize_stages` coerces whatever the planner emitted into a valid,
    executable stage list (invalid patterns degrade to ``single``; dangling
    references are dropped; bounds are clamped) — the planner is an LLM, so the
    engine, not the model, guarantees structural validity.

Everything here is deterministic and I/O-free.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Optional

from . import config
from .models import MAP, REDUCE, REVIEW, SINGLE, STAGE_PATTERNS, StageSpec

MANIFEST_FENCE = "manifest"
_MANIFEST_RE = re.compile(r"```" + MANIFEST_FENCE + r"\s*\n(.*?)```", re.DOTALL)

DELIVERABLE_FENCE = "deliverable"
_DELIVERABLE_RE = re.compile(r"```" + DELIVERABLE_FENCE + r"\s*\n(.*?)```", re.DOTALL)


def deliverable_instruction() -> str:
    """Appended to any stage that produces the artifact-of-record: the exact
    deliverable must be machine-separable from working notes. Observed failure
    this closes: 'exactly one paragraph' shipped as preamble + separator +
    paragraph + sentence-count note + change list, and format criteria were
    judged against the embedded fragment instead of the saved deliverable."""
    return (
        "\nDELIVERABLE PAYLOAD (hard rule): your output produces the run's "
        "deliverable. Wrap the EXACT deliverable content — and nothing else — in a "
        "fenced block:\n```" + DELIVERABLE_FENCE + "\n<the deliverable, verbatim>\n```\n"
        "Everything outside the fence is working notes (explanations, counts, change "
        "lists) and will NOT be delivered. MEASUREMENT TARGET: format constraints "
        "('exactly one paragraph', 'under 40 words', '3 sections') are measured by "
        "machine against the deliverable OF RECORD — the artifact FILE when your "
        "deliverable is a produced file (make the FILE satisfy the counts; the fenced "
        "payload may then faithfully summarize or point to it), otherwise the fenced "
        "payload itself. COUNTING RULE for sections: they are counted at the body's "
        "top heading level; a single leading shallower heading is the document TITLE "
        "and is NOT a section — so 'exactly 5 sections' means a title plus five "
        "same-level headed sections is compliant, and process narration prepended as "
        "an extra heading is a violation.\n"
        "SELF-CHECK BEFORE HANDOFF: if you produced an artifact file an audience "
        "will look at, LOOK at it yourself first — render it to images if a "
        "rendering tool is available, or open produced images with read_file — and "
        "fix what you see (text overflow or clipping, unreadable or dishonest "
        "charts, placeholder text, editorial notes addressed to teammates, wrong "
        "page/slide count) before finishing. Do not hand off work you have not "
        "perceived the way its audience will."
    )


def working_notes(text: str) -> str:
    """Everything OUTSIDE the deliverable fence — the stage's operational
    notes. Fix rounds record tool-behavior discoveries here ('this tool adds
    a cover slide automatically'); a later fix round that never sees them
    re-learns the lesson by repeating the mistake."""
    if not text:
        return ""
    return _DELIVERABLE_RE.sub(" [deliverable payload omitted] ", text).strip()


def extract_deliverable(text: str):
    """The LAST fenced deliverable block, or None when the stage did not emit
    one (caller falls back to the whole output — degraded, never lossy)."""
    if not text:
        return None
    matches = _DELIVERABLE_RE.findall(text)
    if not matches:
        return None
    payload = matches[-1].strip()
    return payload or None


def deliverable_stage_ids(stages: list["StageSpec"]) -> set[str]:
    """Stages whose output IS the artifact-of-record. Planner-marked
    (`deliverable: true`) stages are authoritative when present — ownership is
    plan-level knowledge and must not drift to whatever stage lands last after
    a mid-run extension (an INSPECTION REPORT was observed shipping as the
    deliverable). Fallback: the draft each review verifies (deps[0]) and the
    terminal stage of the graph."""
    marked = {s.id for s in stages if getattr(s, "deliverable", False)}
    if marked:
        return marked
    ids = {s.depends_on[0] for s in stages if s.pattern == REVIEW and s.depends_on}
    ordered = topo_order(stages)
    for s in reversed(ordered):
        if s.pattern != REVIEW:
            ids.add(s.id)
            break
    return ids


def manifest_instruction() -> str:
    """Standard instruction appended to any stage whose output feeds a map."""
    return (
        "\nIMPORTANT — your output will be divided among parallel workers. End your "
        "response with a machine-readable manifest of the work items you found "
        "(one entry per item: a string or a small JSON object), in exactly this form:\n"
        "```" + MANIFEST_FENCE + "\n"
        '["item 1", "item 2", ...]\n'
        "```\n"
        "Include every distinct item; do not summarize items away."
    )


def parse_manifest(text: str) -> Optional[list]:
    """Extract the LAST fenced manifest block from an agent's output.

    Returns None when there is no parseable manifest (caller decides the
    degraded behavior); [] is a valid, honest "nothing found".
    """
    if not text:
        return None
    matches = _MANIFEST_RE.findall(text)
    for raw in reversed(matches):
        try:
            items = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(items, list):
            return items
    return None


def render_item(item: Any) -> str:
    return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)


def shard(items: list, size: int, max_workers: int) -> list[list]:
    """Split items into per-worker groups of ~`size`, never exceeding
    `max_workers` — shards widen instead."""
    if not items:
        return []
    size = max(1, size)
    max_workers = max(1, max_workers)
    if math.ceil(len(items) / size) > max_workers:
        size = math.ceil(len(items) / max_workers)
    return [items[i:i + size] for i in range(0, len(items), size)]


def normalize_stages(raw: list) -> tuple[list[StageSpec], list[str]]:
    """Coerce planner output into executable stages; returns (stages, warnings)."""
    stages: list[StageSpec] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for i, r in enumerate(raw or []):
        if not isinstance(r, dict):
            continue
        sid = str(r.get("id") or f"stage{i + 1}").strip() or f"stage{i + 1}"
        if sid in seen:
            new = f"{sid}-{i + 1}"
            warnings.append(f"duplicate stage id '{sid}' renamed to '{new}'")
            sid = new
        seen.add(sid)
        pattern = str(r.get("pattern") or SINGLE).strip().lower()
        if pattern not in STAGE_PATTERNS:
            warnings.append(f"stage '{sid}': unknown pattern '{pattern}' degraded to single")
            pattern = SINGLE
        stages.append(StageSpec(
            id=sid,
            pattern=pattern,
            role=str(r.get("role") or sid),
            goal=str(r.get("goal") or ""),
            capabilities=[str(c) for c in (r.get("capabilities") or [])],
            depends_on=[str(d) for d in (r.get("depends_on") or [])],
            shard_size=int(r.get("shard_size") or 0),
            shard_from=(str(r["shard_from"]) if r.get("shard_from") else None),
            # A 1-round review can only complain, never fix: unspecified rounds
            # on a review default to 2 (verdict -> one fix cycle -> re-verdict).
            rounds=int(r.get("rounds") or (2 if pattern == REVIEW else 1)),
            expectation=str(r.get("expectation") or ""),
            deliverable=bool(r.get("deliverable")) and pattern != REVIEW,
        ))

    ids = {s.id for s in stages}
    for idx, s in enumerate(stages):
        dangling = [d for d in s.depends_on if d not in ids or d == s.id]
        if dangling:
            warnings.append(f"stage '{s.id}': dropped unknown dependencies {dangling}")
            s.depends_on = [d for d in s.depends_on if d in ids and d != s.id]
        if s.pattern in (MAP, REDUCE, REVIEW) and not s.depends_on:
            if idx > 0:
                prev = stages[idx - 1].id
                warnings.append(f"stage '{s.id}' ({s.pattern}) had no dependencies; chained to '{prev}'")
                s.depends_on = [prev]
            else:
                warnings.append(f"stage '{s.id}' ({s.pattern}) has nothing upstream; degraded to single")
                s.pattern = SINGLE
        if s.pattern == MAP:
            if s.shard_from and s.shard_from not in s.depends_on:
                warnings.append(f"stage '{s.id}': shard_from '{s.shard_from}' is not a dependency; using first dependency")
                s.shard_from = None
            if not s.shard_from:
                s.shard_from = s.depends_on[0] if s.depends_on else None
            if s.shard_size <= 0:
                s.shard_size = config.MAP_DEFAULT_SHARD_SIZE
        # A review's round budget must let it ACT on its own first verdict:
        # verdict + one fix cycle + re-verdict needs 2 rounds, and an explicit
        # planner `rounds: 1` (its contract reads as "fix cycles", off by one
        # from these verdict rounds) was observed turning every first-round
        # "revise" into instant exhaustion — the critique written, never
        # applied, the run dead unverified. The floor guarantees capacity,
        # not acceptance: approval still requires an approved verdict.
        floor = min(2, config.REVIEW_MAX_ROUNDS) if s.pattern == REVIEW else 1
        s.rounds = max(floor, min(s.rounds, config.REVIEW_MAX_ROUNDS))

    return stages, warnings


def append_review_if_needed(stages: list[StageSpec], *, has_user_evidence: bool,
                            has_external_evidence: bool = False
                            ) -> tuple[list[StageSpec], bool]:
    """Risk-scaled review (#13), enforced structurally rather than by rubric hope.

    A grounding review is appended (if the plan lacks one) when the run derives
    factual claims from a USER-REFERENCED SOURCE — an attached file (the shape
    that produced the ebook confabulation) or an external locator named in the
    objective (URL / remote source: a one-stage summary of a linked source was
    observed classified 'grounded' although nothing ever checked its claims) —
    or when the transformation chain is very deep. Returns (stages, appended)."""
    if any(s.pattern == REVIEW for s in stages):
        return stages, False
    deep = _transformation_depth(stages) >= config.REVIEW_DEPTH_THRESHOLD
    if not (has_user_evidence or has_external_evidence or deep):
        return stages, False
    ordered = topo_order(stages)
    if not ordered:
        return stages, False
    stages = list(stages) + [StageSpec(
        id="review", pattern=REVIEW, role="Grounding reviewer",
        goal="Verify the deliverable is grounded in the source evidence and meets the "
             "objective's success criteria; flag any claim not traceable to real inputs "
             "and any source-identity mismatch.",
        depends_on=[ordered[-1].id], rounds=2)]
    return stages, True


def _transformation_depth(stages: list[StageSpec]) -> int:
    """Longest dependency chain — how many times information is transformed from
    source to deliverable. Each hop is a lossy step that can distort meaning."""
    by_id = {s.id: s for s in stages}
    memo: dict[str, int] = {}

    def depth(sid: str, seen: frozenset) -> int:
        if sid in memo:
            return memo[sid]
        s = by_id.get(sid)
        if s is None or sid in seen:
            return 0
        deps = [d for d in s.depends_on if d in by_id]
        d = 1 + (max((depth(dep, seen | {sid}) for dep in deps), default=0))
        memo[sid] = d
        return d

    return max((depth(s.id, frozenset()) for s in stages), default=0)


def topo_order(stages: list[StageSpec]) -> list[StageSpec]:
    """Dependency order (tolerant DFS; ignores cycles rather than failing)."""
    by_id = {s.id: s for s in stages}
    order: list[StageSpec] = []
    visited: set[str] = set()
    on_stack: set[str] = set()

    def visit(s: StageSpec) -> None:
        if s.id in visited or s.id in on_stack:
            return
        on_stack.add(s.id)
        for dep in s.depends_on:
            if dep in by_id:
                visit(by_id[dep])
        on_stack.discard(s.id)
        visited.add(s.id)
        order.append(s)

    for s in stages:
        visit(s)
    return order


def feeds_a_map(stage_id: str, stages: list[StageSpec]) -> bool:
    """True when some map stage shards this stage's output."""
    for s in stages:
        if s.pattern != MAP:
            continue
        src = s.shard_from or (s.depends_on[0] if s.depends_on else None)
        if src == stage_id:
            return True
    return False
