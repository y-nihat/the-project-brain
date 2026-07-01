"""Capability acquisition: research -> build-in-sandbox -> verify -> promote.

Two paths:
  * **Recipe path** (bootstrap): a small set of known, deterministic recipes
    (`web_search`). These are scaffolding while runtime authoring matures.
  * **Runtime authoring path** (the self-acquire pillar): for any capability the
    brain invents that has no recipe, a *developer agent* writes the Python tool
    at runtime; the brain builds it in a sandbox, verifies it generically, and
    (with a self-repair loop) promotes it. Everything is stored under ``data/``.

Free-first throughout. The human approval gate is reached ONLY when a tool
fundamentally needs a credential / account / paid service (no free path).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Awaitable, Callable, Optional

from . import factory
from .models import Capability
from .observability import NullTracer, trace_query
from .sandbox import policy
from .verifier import VerificationResult, verify

CAP_IMAGE_PREFIX = "brain/cap-"


class AcquisitionError(RuntimeError):
    pass


def _write_context(directory: Path, build_context: dict) -> None:
    """Save a {filename: content} build context to disk (a run artifact)."""
    for filename, content in build_context.items():
        (directory / filename).write_text(content)


# --------------------------------------------------------------------------- #
# Bootstrap recipe: web_search (deterministic, known-good scaffolding)
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
        network="bridge",
        tags=["web_search", "research"],
        verified=False,
    )


def web_search_build_context() -> dict[str, str]:
    return {"Dockerfile": WEB_SEARCH_DOCKERFILE, "search.py": WEB_SEARCH_PROGRAM}


_RECIPES: dict[str, tuple] = {
    "web_search": (web_search_capability, web_search_build_context, "ddgs (DuckDuckGo, no API key)"),
}


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
Builder = Callable[[str, str], Awaitable[Optional[dict]]]
Developer = Callable[..., Awaitable[Optional[dict]]]


async def acquire(
    need_key: str,
    *,
    manager,
    registry,
    broker,
    builder: Optional[Builder] = None,
    developer: Optional[Developer] = None,
    description: Optional[str] = None,
    model: Optional[str] = None,
    max_attempts: int = 3,
    tracer=None,
) -> Capability:
    """Acquire the capability for ``need_key`` (recipe if known, else author it)."""
    tracer = tracer or NullTracer()
    recipe = _RECIPES.get(need_key)
    if recipe is not None:
        return await _acquire_from_recipe(
            need_key, recipe, manager=manager, registry=registry,
            builder=builder, max_attempts=max_attempts, tracer=tracer,
        )
    return await _author_capability(
        need_key, description or need_key, manager=manager, registry=registry, broker=broker,
        model=model, developer=developer, max_attempts=max_attempts, tracer=tracer,
    )


# --------------------------------------------------------------------------- #
# Recipe path (bootstrap)
# --------------------------------------------------------------------------- #
async def _acquire_from_recipe(
    need_key, recipe, *, manager, registry, builder, max_attempts, tracer
) -> Capability:
    make_cap, make_context, free_hint = recipe
    cap: Capability = make_cap()

    with tracer.span("capability.acquire", need_key, mode="recipe", free_path=free_hint):
        tracer.event("decision", f"acquire {need_key}", status="info",
                     choice="create", capability=need_key, free_path=free_hint)

        last_detail = ""
        build_context: Optional[dict] = None
        for attempt in range(1, max_attempts + 1):
            source = "recipe"
            if builder is not None and attempt == 1:
                try:
                    generated = await builder(need_key, free_hint)
                except Exception as e:  # noqa: BLE001
                    last_detail = f"builder agent error: {e}"
                    generated = None
                if generated:
                    build_context = generated
                    source = "agent"
                else:
                    build_context = make_context()
            else:
                build_context = make_context()

            attempt_dir = tracer.artifact_path(cap.name, f"attempt-{attempt}-{source}")
            _write_context(attempt_dir, build_context)

            with tracer.span("capability.build", cap.name, image=cap.image_tag,
                             attempt=attempt, source=source, artifact_path=str(attempt_dir)):
                await asyncio.to_thread(manager.build_image, cap.image_tag, build_context)

            with tracer.span("capability.verify", cap.name) as vsp:
                result: VerificationResult = await asyncio.to_thread(verify, cap, manager)
                vsp.set(passed=result.passed, detail=result.detail, source=source)
                if not result.passed:
                    vsp.status = "failed"

            if result.passed:
                cap.verified = True
                promoted_dir = tracer.artifact_path(cap.name, "promoted")
                _write_context(promoted_dir, build_context)
                with tracer.span("capability.promote", cap.name) as psp:
                    registry.add(cap, build_context=build_context)
                    psp.set(source=source,
                            code_path=str(Path(registry.store_dir) / cap.name / "build"),
                            artifact_path=str(promoted_dir))
                return cap

            last_detail = result.detail
            build_context = None

        raise AcquisitionError(f"could not build a working '{need_key}': {last_detail}")


# --------------------------------------------------------------------------- #
# Runtime authoring path (the self-acquire pillar)
# --------------------------------------------------------------------------- #
def _capability_from_submission(need_key: str, description: str, sub: dict) -> Capability:
    return Capability(
        name=need_key,
        description=(description or need_key)[:500],
        image_tag=f"{CAP_IMAGE_PREFIX}{need_key}:1.0.0",
        command=list(sub.get("command") or ["python", "-B", "/app/tool.py"]),
        input_schema=sub.get("input_schema") or {"type": "object", "properties": {}},
        env_map=sub.get("env_map") or {},
        network="bridge" if sub.get("network") == "bridge" else "none",
        tags=["authored"],
        verified=False,
    )


def _generic_verify(cap: Capability, sample_input: dict, sample_files: dict, manager) -> tuple[bool, str]:
    """Generic acceptance: run the tool on a sample input (plus optional sample
    files placed under /work/in); success = exit 0 and a parseable, non-error JSON
    object on the last stdout line."""
    args = dict(sample_input or {})
    args.pop("files", None)  # reserved runtime arg, not an env input
    env = {}
    for key, val in args.items():
        if key in cap.env_map:
            if isinstance(val, str):
                env[cap.env_map[key]] = val
            elif isinstance(val, (int, float, bool)):
                env[cap.env_map[key]] = str(val)
            else:
                env[cap.env_map[key]] = json.dumps(val)
    input_files = {
        name: (content.encode("utf-8") if isinstance(content, str) else bytes(content))
        for name, content in (sample_files or {}).items()
    }
    pol = policy.egress() if cap.network == "bridge" else policy.isolated()
    res = manager.run_ephemeral(
        cap.image_tag, cap.command, policy=pol, capability=cap.name, environment=env,
        input_files=input_files or None, capture_output_files=True, ttl=120,
    )
    if not res.ok:
        return (False, f"exit={res.exit_code} timed_out={res.timed_out}: {(res.stderr or '')[:300]}")
    out = (res.stdout or "").strip()
    if not out:
        return (False, "no stdout output")
    try:
        data = json.loads(out.splitlines()[-1])
    except Exception as e:  # noqa: BLE001
        return (False, f"last stdout line is not JSON ({e}): {out[:200]!r}")
    if isinstance(data, dict) and set(data.keys()) == {"error"}:
        return (False, f"tool reported error: {data['error']}")
    return (True, f"ok: {str(data)[:120]}")


async def _author_capability(
    need_key, description, *, manager, registry, broker, model, developer, max_attempts, tracer
) -> Capability:
    dev = developer or make_developer_agent(model, tracer)

    with tracer.span("capability.acquire", need_key, mode="author", description=description):
        tracer.event("decision", f"author {need_key}", status="info",
                     choice="author", capability=need_key)

        previous: Optional[str] = None
        error: Optional[str] = None
        last_detail = "no submission"

        for attempt in range(1, max_attempts + 1):
            sub = await dev(need_key, description, previous, error)
            if not sub or not sub.get("program") or not sub.get("dockerfile"):
                last_detail = "developer produced no usable submission"
                error = last_detail
                continue

            # Free-first: escalate to the human ONLY if the tool needs a credential.
            rc = sub.get("requires_credential")
            if rc:
                approved = broker.request(
                    action=f"build capability '{need_key}', which needs {rc}",
                    reason="the tool fundamentally requires a credential/account/paid service (no free path)",
                    requirements=[str(rc)],
                )
                if not approved:
                    raise AcquisitionError(f"approval denied: '{need_key}' needs {rc}")
                raise AcquisitionError(
                    f"'{need_key}' needs a credential ({rc}); secret provisioning is not yet supported"
                )

            cap = _capability_from_submission(need_key, description, sub)
            build_context = {"Dockerfile": sub["dockerfile"], "tool.py": sub["program"]}
            attempt_dir = tracer.artifact_path(cap.name, f"attempt-{attempt}-author")
            _write_context(attempt_dir, build_context)

            try:
                with tracer.span("capability.build", cap.name, image=cap.image_tag,
                                 attempt=attempt, source="author", artifact_path=str(attempt_dir)):
                    await asyncio.to_thread(manager.build_image, cap.image_tag, build_context)
            except Exception as e:  # noqa: BLE001  (build failure -> self-repair)
                last_detail = f"build failed: {e}"
                error = str(e)[:1500]
                previous = sub["program"]
                continue

            with tracer.span("capability.verify", cap.name) as vsp:
                ok, detail = await asyncio.to_thread(
                    _generic_verify, cap, sub.get("sample_input") or {},
                    sub.get("sample_files") or {}, manager,
                )
                vsp.set(passed=ok, detail=detail, source="author")
                if not ok:
                    vsp.status = "failed"

            if ok:
                cap.verified = True
                promoted_dir = tracer.artifact_path(cap.name, "promoted")
                _write_context(promoted_dir, build_context)
                with tracer.span("capability.promote", cap.name) as psp:
                    registry.add(cap, build_context=build_context)
                    psp.set(source="author",
                            code_path=str(Path(registry.store_dir) / cap.name / "build"),
                            artifact_path=str(promoted_dir))
                return cap

            last_detail = detail
            error = detail
            previous = sub["program"]

        raise AcquisitionError(f"could not author a working '{need_key}': {last_detail}")


# --------------------------------------------------------------------------- #
# Agents (SDK-driven)
# --------------------------------------------------------------------------- #
_BUILDER_SYSTEM = (
    "You build small, single-purpose tools that run in an isolated container. Produce a capability "
    "that searches the live web using a FREE library that needs NO API key (suggested: {free_hint}). "
    "Requirements for the program:\n"
    "  * read the query from env var SEARCH_QUERY and an optional integer SEARCH_MAX_RESULTS;\n"
    "  * print exactly one JSON object on the final stdout line: "
    "{{\"query\": <str>, \"results\": [{{\"title\": <str>, \"href\": <str>, \"body\": <str>}}]}};\n"
    "  * exit 0 on success, non-zero on failure.\n"
    "Requirements for the Dockerfile: base image python:3.12-slim, pip install the chosen library, "
    "and `COPY search.py /app/search.py`. Do NOT set an ENTRYPOINT/CMD. "
    "You MUST call the `submit_build` tool exactly once with both files and must not answer in prose."
)

_BUILDER_TASK = "Build the web_search capability now and submit it."


def make_builder_agent(model: Optional[str] = None, tracer=None) -> Builder:
    tracer = tracer or NullTracer()

    async def builder(need_key: str, free_hint: str) -> Optional[dict]:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        captured: dict = {}

        @tool(
            "submit_build",
            "Submit the Dockerfile and the Python program for the capability.",
            {
                "type": "object",
                "properties": {"dockerfile": {"type": "string"}, "program": {"type": "string"}},
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
        await trace_query(tracer, "builder", _BUILDER_TASK, options)

        if "Dockerfile" in captured and "search.py" in captured:
            return {"Dockerfile": captured["Dockerfile"], "search.py": captured["search.py"]}
        return None

    return builder


# The developer authors ARBITRARY tools to a generic contract. Literal JSON braces
# are fine here because this string is NOT passed through str.format().
_DEVELOPER_SYSTEM = (
    "You are a developer agent. You write a small, single-purpose command-line TOOL in Python that "
    "runs inside an isolated Docker sandbox. Use ONLY free, open-source libraries that need NO API "
    "key or account.\n\n"
    "The tool contract (follow it exactly):\n"
    "  * Scalar inputs come from environment variables (you define env_map: arg_name -> ENV_VAR_NAME). "
    "Env values are strings; JSON-decode any non-scalar argument.\n"
    "  * If your tool needs input FILES, read ALL files under /work/in/. To let callers attach files, "
    "declare an argument named `files` (array of strings) in input_schema; the system places the "
    "referenced files under /work/in/ (do NOT read `files` from the environment).\n"
    "  * Write any OUTPUT files under /work/out/ (create the directory if needed).\n"
    "  * ALWAYS print EXACTLY ONE JSON object as the LAST line of stdout summarizing the result "
    "(e.g. the answer, or the list of files you created). On failure, print "
    '{"error": "<reason>"} and exit non-zero. Exit 0 on success.\n\n'
    "Submit via the `submit_tool` tool with:\n"
    "  * program: the full Python source (saved as /app/tool.py).\n"
    "  * dockerfile: base python:3.12-slim, `pip install` the libs you need, and "
    "`COPY tool.py /app/tool.py`. Do NOT set ENTRYPOINT/CMD.\n"
    "  * command: how to run it, e.g. [\"python\", \"-B\", \"/app/tool.py\"].\n"
    "  * input_schema: JSON schema of the tool's arguments (include `files` if it consumes files).\n"
    "  * env_map: object mapping each scalar argument name to its ENV_VAR_NAME.\n"
    "  * network: \"none\" if the tool needs no internet while running, \"bridge\" if it does.\n"
    "  * sample_input: concrete arguments that should make the tool succeed (used to verify it).\n"
    "  * sample_files: an optional map {filename: text_content} placed under /work/in/ so a "
    "file-consuming tool can be verified.\n"
    "  * requires_credential: set to a short string ONLY IF the tool fundamentally needs an API "
    "key/account/paid service (no free path); otherwise null.\n\n"
    "Prefer well-known, currently-maintained libraries. You MUST call `submit_tool` exactly once and "
    "must not answer in prose."
)

_SUBMIT_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "program": {"type": "string"},
        "dockerfile": {"type": "string"},
        "command": {"type": "array", "items": {"type": "string"}},
        "input_schema": {"type": "object"},
        "env_map": {"type": "object"},
        "network": {"type": "string", "enum": ["none", "bridge"]},
        "sample_input": {"type": "object"},
        "sample_files": {"type": "object"},
        "requires_credential": {"type": ["string", "null"]},
    },
    "required": ["program", "dockerfile", "command", "input_schema", "env_map", "network", "sample_input"],
}


def make_developer_agent(model: Optional[str] = None, tracer=None) -> Developer:
    tracer = tracer or NullTracer()

    async def developer(need_key, description, previous=None, error=None) -> Optional[dict]:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        captured: dict = {}

        @tool("submit_tool", "Submit the tool implementation and metadata.", _SUBMIT_TOOL_SCHEMA)
        async def submit_tool(args):
            captured.update(args)
            return {"content": [{"type": "text", "text": "received"}]}

        server = create_sdk_mcp_server(name="dev", version="1.0.0", tools=[submit_tool])
        options = factory.locked_session(
            system_prompt=_DEVELOPER_SYSTEM,
            mcp_servers={"dev": server},
            allowed_tools=["mcp__dev__submit_tool"],
            model=model,
            max_turns=4,
        )

        task = (
            f"Build a tool named '{need_key}'. Purpose: {description}\n"
            "Follow the contract and submit it now."
        )
        if error:
            task += (
                "\n\nYour PREVIOUS attempt FAILED.\n"
                f"Previous program:\n{(previous or '')[:4000]}\n"
                f"Error:\n{error[:2000]}\n"
                "Diagnose the failure and submit a corrected tool."
            )

        await trace_query(tracer, f"developer:{need_key}", task, options)
        return dict(captured) if captured.get("program") and captured.get("dockerfile") else None

    return developer
