"""Capability-acquisition loop: research -> build-in-sandbox -> verify -> promote.

Free-first is enforced here: a need with a known free, keyless recipe is built
without bothering the human; a need with no free path STOPS at the approval gate.

The build context can be produced two ways:
  * a *builder agent* (the headline self-acquisition behavior) that generates the
    Dockerfile + program, or
  * a deterministic *recipe* (known-good fallback, and the path used by tests).

Either way the artifact is built in a sandbox and must pass deterministic
verification before it is promoted into the registry.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from . import factory
from .models import Capability
from .verifier import VerificationResult, verify

CAP_IMAGE_PREFIX = "brain/cap-"


class AcquisitionError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# web_search recipe (deterministic, known-good)
# --------------------------------------------------------------------------- #
WEB_SEARCH_DOCKERFILE = """FROM python:3.12-slim
RUN pip install --no-cache-dir ddgs
WORKDIR /app
COPY search.py /app/search.py
"""

WEB_SEARCH_PROGRAM = r'''import json, os, sys


def main():
    query = os.environ.get("SEARCH_QUERY", "").strip()
    try:
        max_results = int(os.environ.get("SEARCH_MAX_RESULTS", "5"))
    except ValueError:
        max_results = 5
    if not query:
        print(json.dumps({"error": "no query provided"}))
        return 2
    try:
        from ddgs import DDGS
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"import failed: {e}"}))
        return 3
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title": r.get("title"),
                    "href": r.get("href") or r.get("url"),
                    "body": r.get("body") or r.get("snippet"),
                })
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"search failed: {e}", "query": query}))
        return 1
    print(json.dumps({"query": query, "results": results}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def web_search_capability() -> Capability:
    return Capability(
        name="web_search",
        description=(
            "Search the live web via DuckDuckGo (ddgs, no API key). "
            "Input: query (str), optional max_results (int). "
            "Returns JSON {query, results:[{title, href, body}]}."
        ),
        image_tag=f"{CAP_IMAGE_PREFIX}web_search:1.0.0",
        command=["python", "-B", "/app/search.py"],
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "max_results": {"type": "integer", "description": "Max results (default 5)"},
            },
            "required": ["query"],
        },
        env_map={"query": "SEARCH_QUERY", "max_results": "SEARCH_MAX_RESULTS"},
        network="bridge",  # web search needs outbound egress
        tags=["web_search", "research"],
        verified=False,
    )


def web_search_build_context() -> dict[str, str]:
    return {"Dockerfile": WEB_SEARCH_DOCKERFILE, "search.py": WEB_SEARCH_PROGRAM}


# Per-need recipes the brain already knows how to build for free, keylessly.
_RECIPES: dict[str, tuple] = {
    "web_search": (web_search_capability, web_search_build_context, "ddgs (DuckDuckGo, no API key)"),
}

# What a non-free path would require (used only to explain to the human at the gate).
_PAID_ALTERNATIVES = {
    "web_search": ["SerpAPI", "Google Custom Search", "Bing Web Search"],
}


# Type of an optional builder-agent callable.
Builder = Callable[[str, str], Awaitable[Optional[dict]]]


async def acquire(
    need_key: str,
    *,
    manager,
    registry,
    broker,
    builder: Optional[Builder] = None,
    max_attempts: int = 2,
) -> Capability:
    """Acquire (build + verify + promote) the capability for ``need_key``."""
    recipe = _RECIPES.get(need_key)
    if recipe is None:
        # No known free, keyless path -> we must not proceed without the human.
        approved = broker.request(
            action=f"acquire a brand-new capability '{need_key}'",
            reason="the brain has no known free, keyless recipe for this capability yet",
            requirements=[
                "your guidance toward a free approach, or approval for a paid/credentialed service"
            ],
        )
        if not approved:
            raise AcquisitionError(f"approval denied for acquiring '{need_key}'")
        raise AcquisitionError(
            f"no recipe available for '{need_key}'; M1 supports 'web_search' only"
        )

    make_cap, make_context, free_hint = recipe
    # Free-first decision: this need HAS a free, keyless path, so no approval is
    # needed. (If we were forced onto a keyed provider — _PAID_ALTERNATIVES —
    # we would call broker.request() here before going further.)
    cap: Capability = make_cap()

    last_detail = ""
    build_context: Optional[dict] = None
    for attempt in range(1, max_attempts + 1):
        if builder is not None and attempt == 1:
            try:
                build_context = await builder(need_key, free_hint)
            except Exception as e:  # noqa: BLE001
                last_detail = f"builder agent error: {e}"
                build_context = None
        if not build_context:
            build_context = make_context()  # deterministic fallback

        await asyncio.to_thread(manager.build_image, cap.image_tag, build_context)
        result: VerificationResult = await asyncio.to_thread(verify, cap, manager)
        if result.passed:
            cap.verified = True
            registry.add(cap, build_context=build_context)  # promote
            return cap

        last_detail = result.detail
        build_context = None  # force fallback/regeneration on the next attempt

    raise AcquisitionError(f"could not build a working '{need_key}': {last_detail}")


# --------------------------------------------------------------------------- #
# Optional builder agent (self-acquisition). Generates the build context via a
# locked-down LLM session that must submit it through a structured tool.
# --------------------------------------------------------------------------- #
_BUILDER_SYSTEM = (
    "You build small, single-purpose tools that run in an isolated container. Produce a "
    "capability that searches the live web using a FREE library that needs NO API key "
    "(suggested: {free_hint}). Requirements for the program:\n"
    "  * read the query from env var SEARCH_QUERY and an optional integer SEARCH_MAX_RESULTS;\n"
    "  * print exactly one JSON object on the final stdout line: "
    "{{\"query\": <str>, \"results\": [{{\"title\": <str>, \"href\": <str>, \"body\": <str>}}]}};\n"
    "  * exit 0 on success, non-zero on failure.\n"
    "Requirements for the Dockerfile: base image python:3.12-slim, pip install the chosen "
    "library, and `COPY search.py /app/search.py`. Do NOT set an ENTRYPOINT/CMD. "
    "You MUST call the `submit_build` tool exactly once with both files and must not answer in prose."
)

_BUILDER_TASK = "Build the web_search capability now and submit it."


def make_builder_agent(model: Optional[str] = None) -> Builder:
    async def builder(need_key: str, free_hint: str) -> Optional[dict]:
        from claude_agent_sdk import create_sdk_mcp_server, query, tool

        captured: dict = {}

        @tool(
            "submit_build",
            "Submit the Dockerfile and the Python program for the capability.",
            {
                "type": "object",
                "properties": {
                    "dockerfile": {"type": "string"},
                    "program": {"type": "string"},
                },
                "required": ["dockerfile", "program"],
            },
        )
        async def submit_build(args):
            captured["Dockerfile"] = args["dockerfile"]
            captured["search.py"] = args["program"]
            return {"content": [{"type": "text", "text": "received"}]}

        server = create_sdk_mcp_server(name="build", version="1.0.0", tools=[submit_build])
        options = factory.locked_session(
            system_prompt=_BUILDER_SYSTEM.format(free_hint=free_hint),
            mcp_servers={"build": server},
            allowed_tools=["mcp__build__submit_build"],
            model=model,
            max_turns=4,
        )
        async for _ in query(prompt=_BUILDER_TASK, options=options):
            pass

        if "Dockerfile" in captured and "search.py" in captured:
            return {"Dockerfile": captured["Dockerfile"], "search.py": captured["search.py"]}
        return None

    return builder
