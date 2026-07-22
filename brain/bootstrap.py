"""The engine's INNATE needs, declared as data.

The core-loop invariant: capabilities are identified by CONTRACT and resolved
through the same reuse-or-author path whatever the task; the engine never
assumes a tool exists by name and never encodes a task's solution shape. The
few capabilities the engine itself relies on — the observation channel that
delegated research runs on, and the renderer that perceptual review uses —
are therefore declared HERE as contracts: data the loop consumes like any
other need, not names wired into logic. A missing innate capability is
authored from its contract exactly like a plan-declared one; nothing in this
module is a precondition.

Need keys are registry identities (cache/catalog keys), never behavioral
assumptions: no engine code may depend on what a key is called, only on the
contract it maps to.
"""
from __future__ import annotations

# The observation channel: how a research agent reaches the live world. Both
# entries are instance-neutral contracts; `web_search` also has a verified
# bootstrap recipe (acquisition._RECIPES) as its fallback implementation.
# ORDER IS MEANINGFUL — declared SHALLOW to DEEP: earlier keys survey (an
# index of what exists), later keys retrieve the full content a survey only
# points at. primary_observation_need() and the depth nudge both rely on this
# ordering; a channel is "deeper" than another iff it appears later here.
OBSERVATION_NEEDS: dict[str, str] = {
    "web_search": (
        "Search the live web via DuckDuckGo (ddgs, no API key). "
        "Input: query (str), optional max_results (int). "
        "Returns JSON {query, results:[{title, href, body}]}."
    ),
    "fetch_page_text": (
        "Fetch the page or document at a provided URL and extract its "
        "readable text content. Inputs: the URL (and optionally a byte/char "
        "limit). Output: the extracted text, retained as a payload file, "
        "with source_refs naming the URL actually fetched, honest "
        "coverage/observation_state reporting, and unresolved needs for "
        "content that could not be rendered or extracted."
    ),
}


def primary_observation_need() -> str:
    """The single need a degraded-mode fallback stage gets: the first declared
    observation contract (data-derived — never a literal in logic)."""
    return next(iter(OBSERVATION_NEEDS))


# Perceptual review: when an audience will look at an artifact, the reviewer
# must look at it — page-layout formats are rendered to images first.
RENDER_NEED_KEY = "render_artifact_pages"
RENDER_CONTRACT = (
    "Accepts a document/presentation/page-layout file via the files argument. Renders "
    "EVERY page/slide/frame of it to sequentially numbered PNG images (one image per "
    "page, at a resolution where body text is readable) in the output directory. "
    "Returns page_count and the list of image filenames, reports consumed_inputs, and "
    "fails honestly when the format cannot be rendered. Purely a converter — no content "
    "interpretation, no editing, no summarizing.")

# The engine's PRIOR for which formats are page-renderable. A prior, not a
# boundary: renderable_exts() unions it with whatever the promoted renderer
# itself declares it accepts, so a format the system LEARNED to render is
# perceived from then on.
RENDERABLE_EXTS_DEFAULT = frozenset({
    ".pptx", ".ppt", ".odp", ".docx", ".doc", ".odt", ".pdf",
    ".html", ".htm", ".svg", ".rtf"})


def renderable_exts(registry=None) -> frozenset:
    """Engine default ∪ the promoted renderer's own declared ``accepts``.

    The tool the system built extends what the engine perceives — the
    founding "learns through interactions" clause applied to perception.
    Registry absence or failure degrades to the default prior."""
    exts = set(RENDERABLE_EXTS_DEFAULT)
    if registry is not None:
        try:
            cap = registry.get(RENDER_NEED_KEY)
        except Exception:  # noqa: BLE001 — a lookup must never break perception
            cap = None
        if cap is not None and not getattr(cap, "quarantined", False):
            for a in (getattr(cap, "accepts", None) or []):
                a = str(a).strip().lower()
                if a:
                    exts.add(a if a.startswith(".") else "." + a)
    return frozenset(exts)
