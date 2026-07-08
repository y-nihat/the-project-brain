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
import re
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


def _error_tail(text: str, limit: int = 2000) -> str:
    """Keep the END of an error text — tracebacks and pip/build errors live there."""
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


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
    pre_interaction=None,
) -> Capability:
    """Acquire the capability for ``need_key`` (recipe if known, else author it).

    ``pre_interaction`` (async str -> None) is consulted before any user-facing
    ask, so the supervisor can equip the user first."""
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
        pre_interaction=pre_interaction,
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
    resources = sub.get("resources") or {}

    def _num(key, conv):
        try:
            v = resources.get(key)
            return conv(v) if v else None
        except (TypeError, ValueError):
            return None

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
        memory_mb=_num("memory_mb", int),
        cpus=_num("cpus", float),
        timeout_seconds=_num("timeout_seconds", int),
    )


def _generic_verify(cap: Capability, sample_input: dict, sample_files: dict, manager,
                    extra_env: Optional[dict] = None):
    """Generic acceptance: run the tool on a sample input (plus optional sample
    files placed under /work/in); success = exit 0 and a parseable, non-error JSON
    object on the last stdout line. Returns (ok, detail, run_result) — detail
    carries the TAIL of stderr (the end is where tracebacks live).

    ``extra_env`` carries session secrets (credential env vars) so a
    credentialed tool can be verified for real; values are never traced."""
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
    # Session secrets LAST so the real credential always wins over any
    # placeholder a sample_input smuggled in under the same env var.
    env.update(extra_env or {})
    input_files = {
        name: (content.encode("utf-8") if isinstance(content, str) else bytes(content))
        for name, content in (sample_files or {}).items()
    }
    pol = policy.for_capability(cap.memory_mb, cap.cpus, cap.timeout_seconds, cap.network)
    res = manager.run_ephemeral(
        cap.image_tag, cap.command, policy=pol, capability=cap.name, environment=env,
        input_files=input_files or None, capture_output_files=True,
    )
    if not res.ok:
        if res.oom:
            detail = (
                f"KILLED: the container exceeded its memory limit ({pol.mem}) and was OOM-killed "
                "by the kernel — no traceback exists for this failure mode. Reduce memory use "
                "(smaller model/library, streaming instead of loading everything) or declare "
                "higher `resources.memory_mb` (the engine clamps to its ceiling)."
            )
            return (False, detail, res)
        detail = (
            f"exit={res.exit_code} timed_out={res.timed_out}; "
            f"env vars provided: {sorted(env)}; "
            f"stderr (tail): {_error_tail(res.stderr, 1200)}; "
            f"stdout (tail): {_error_tail(res.stdout, 400)}"
        )
        return (False, detail, res)
    out = (res.stdout or "").strip()
    if not out:
        return (False, "no stdout output", res)
    try:
        data = json.loads(out.splitlines()[-1])
    except Exception as e:  # noqa: BLE001
        return (False, f"last stdout line is not JSON ({e}): {_error_tail(out, 400)!r}", res)
    if isinstance(data, dict) and "error" in data:
        return (False, f"tool reported error: {str(data['error'])[:400]} "
                       f"(env vars provided: {sorted(env)})", res)
    return (True, f"ok: {str(data)[:120]}", res)


async def _author_capability(
    need_key, description, *, manager, registry, broker, model, developer, max_attempts, tracer,
    seed: Optional[dict] = None, repair_reason: Optional[str] = None, pre_interaction=None,
) -> Capability:
    dev = developer or make_developer_agent(model, tracer)
    mode = "repair" if seed else "author"

    with tracer.span("capability.acquire", need_key, mode=mode, description=description):
        tracer.event("decision", f"{mode} {need_key}", status="info",
                     choice=mode, capability=need_key)

        # Repair mode seeds the developer with the current tool + the live failure,
        # so its first attempt is a fix (reusing the self-repair machinery).
        previous: Optional[dict] = seed   # {"program":…, "dockerfile":…}
        error: Optional[str] = (
            f"The tool builds and runs but FAILS IN PRACTICE: {repair_reason}. Improve it — add "
            "retries/backoff, pace requests to avoid rate limits, handle empty responses, or switch "
            "to an ALTERNATIVE library/endpoint. Keep the SAME input/output contract (same env vars, "
            "same JSON shape)." if seed else None
        )
        last_detail = "no submission"

        for attempt in range(1, max_attempts + 1):
            sub = await dev(need_key, description, previous, error)
            if not sub or not sub.get("program") or not sub.get("dockerfile"):
                last_detail = "developer produced no usable submission"
                error = last_detail
                continue

            # Free-first: escalate to the human ONLY if the tool needs a credential.
            rc = sub.get("requires_credential")
            secret_env: dict[str, str] = {}
            if rc:
                # Canonical env-var name: developers drift between spellings
                # across attempts ("service_api_key" vs SERVICE_API_KEY), which
                # double-prompts the user. One name, one ask, cached.
                rc = re.sub(r"\W+", "_", str(rc).strip()).upper().strip("_")
                if rc in broker.secrets:
                    secret_env[rc] = broker.secrets[rc]
                else:
                    if pre_interaction is not None:
                        await pre_interaction(
                            f"approval + hidden prompt for credential {rc}, needed to build the "
                            f"tool '{need_key}' ({description[:100]}); author-provided retrieval "
                            f"instructions: {'yes' if sub.get('credential_help') else 'NO — none'}")
                    approved = broker.request(
                        action=f"build capability '{need_key}', which needs {rc}",
                        reason="the tool fundamentally requires a credential/account/paid service (no free path)",
                        requirements=[rc],
                    )
                    if not approved:
                        raise AcquisitionError(f"approval denied: '{need_key}' needs {rc}")
                    secret = broker.collect_secret(
                        rc, purpose=f"capability '{need_key}' ({description[:80]})",
                        help_text=sub.get("credential_help"))
                    if not secret:
                        raise AcquisitionError(f"'{need_key}' needs {rc}, but no value was provided")
                    secret_env[rc] = secret

            cap = _capability_from_submission(need_key, description, sub)
            cap.requires_credential = rc or None
            cap.credential_help = (str(sub.get("credential_help")).strip()[:2000]
                                   if rc and sub.get("credential_help") else None)
            build_context = {"Dockerfile": sub["dockerfile"], "tool.py": sub["program"]}
            attempt_dir = tracer.artifact_path(cap.name, f"attempt-{attempt}-author")
            _write_context(attempt_dir, build_context)

            try:
                with tracer.span("capability.build", cap.name, image=cap.image_tag,
                                 attempt=attempt, source="author", artifact_path=str(attempt_dir)):
                    await asyncio.to_thread(manager.build_image, cap.image_tag, build_context)
            except Exception as e:  # noqa: BLE001  (build failure -> self-repair)
                # Persist the full log for post-mortems; feed the developer the
                # TAIL (the end is where the real error lives).
                full_log = getattr(e, "full_log", "") or str(e)
                (attempt_dir / "build.log").write_text(full_log)
                tail = _error_tail(str(e))
                last_detail = f"build failed: {tail}"
                error = f"The docker image BUILD failed. Error (tail of build log):\n{tail}"
                previous = {"program": sub["program"], "dockerfile": sub["dockerfile"]}
                continue

            with tracer.span("capability.verify", cap.name) as vsp:
                ok, detail, run_res = await asyncio.to_thread(
                    _generic_verify, cap, sub.get("sample_input") or {},
                    sub.get("sample_files") or {}, manager, secret_env or None,
                )
                vsp.set(passed=ok, detail=detail, source="author")
                if not ok:
                    vsp.status = "failed"
                    if run_res is not None:
                        (attempt_dir / "verify.stderr").write_text(run_res.stderr or "")
                        (attempt_dir / "verify.stdout").write_text(run_res.stdout or "")

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
            error = f"The image built, but VERIFICATION of the running tool failed:\n{detail}"
            previous = {"program": sub["program"], "dockerfile": sub["dockerfile"]}

        raise AcquisitionError(f"could not author a working '{need_key}': {last_detail}")


def _load_seed(registry, cap: Capability) -> Optional[dict]:
    """The current promoted build context (program + Dockerfile), to seed a repair."""
    build_dir = Path(registry.store_dir) / cap.name / "build"
    dockerfile = build_dir / "Dockerfile"
    pys = sorted(build_dir.glob("*.py"))
    if not dockerfile.exists() or not pys:
        return None
    return {"dockerfile": dockerfile.read_text(), "program": pys[0].read_text()}


async def repair_capability(
    cap: Capability, reason: str, *, manager, registry, broker,
    model: Optional[str] = None, developer: Optional[Developer] = None,
    max_attempts: int = 2, tracer=None,
) -> Capability:
    """Repair a trusted-but-degraded tool in place: re-author it (seeded with its
    current code + the live failure), re-verify, and re-promote under the same
    image tag so the re-running agent transparently gets the improved tool."""
    tracer = tracer or NullTracer()
    seed = _load_seed(registry, cap)
    repaired = await _author_capability(
        cap.name, cap.description, manager=manager, registry=registry, broker=broker,
        model=model, developer=developer, max_attempts=max_attempts, tracer=tracer,
        seed=seed, repair_reason=reason,
    )
    # Record a bumped version for provenance (image tag stays constant on purpose).
    try:
        major, minor, patch = (cap.version or "1.0.0").split(".")
        repaired.version = f"{major}.{minor}.{int(patch) + 1}"
        registry.add(repaired)
    except Exception:  # noqa: BLE001
        pass
    return repaired


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
    "  * resources: optional {memory_mb, cpus, timeout_seconds} — declare HONESTLY what the tool "
    "needs. Defaults are 256 MB / 0.5 CPU / 120 s; the engine clamps declarations to hard ceilings "
    "(2048 MB / 2 CPU / 300 s). Memory is a HARD cap with no swap: exceeding it means the kernel "
    "KILLS the process with no traceback. Model-loading libraries (torch, transformers) need "
    "1024+ MB — declare it.\n"
    "  * requires_credential: ONLY IF the tool fundamentally needs an API key/account/paid "
    "service (no free path), set this to the UPPERCASE ENV VAR NAME of the credential "
    "(e.g. SERVICE_API_KEY); otherwise null. The engine collects the REAL value from the "
    "human and injects it as that exact environment variable at BOTH verify time and run time, "
    "so: your program MUST read the credential from exactly that env var; do NOT declare the "
    "credential as a tool argument in input_schema/env_map; and NEVER put a credential value "
    "(real or placeholder) in sample_input — verification runs with the real injected value, "
    "and a placeholder will make it fail (e.g. 401).\n"
    "  * credential_help: REQUIRED whenever requires_credential is set — short numbered steps "
    "a HUMAN follows to obtain that credential (where to register, which console/menu/command, "
    "what the value looks like). The engine shows this to the user BEFORE prompting for the "
    "value. Write it for the end user, not for developers.\n\n"
    "Tools run NON-INTERACTIVELY in an isolated sandbox: they can never prompt, ask, or wait "
    "for the user (no stdin, no UI). NEVER author a tool whose job is to collect input, "
    "credentials, or confirmation from a human — declare requires_credential instead and the "
    "engine collects the value.\n"
    "Sandbox facts (respect these — they are why tools fail):\n"
    "  * The image BUILD always has internet access; at RUNTIME the container has internet ONLY if "
    "you set network=\"bridge\".\n"
    "  * At runtime HOME=/tmp. Anything cached under /root during the build (e.g. default model/data "
    "caches) will NOT be found at runtime. If you bake models or data into the image, store them "
    "under an explicit path such as /app/models and point the library there with ENV lines in the "
    "Dockerfile (e.g. ENV SENTENCE_TRANSFORMERS_HOME=/app/models).\n"
    "  * On crash, let the real exception reach stderr — never swallow tracebacks.\n\n"
    "Dependency rules:\n"
    "  * NEVER put --index-url on a pip install line that also installs normal PyPI packages — it "
    "REPLACES PyPI and those packages will not be found. Use a separate RUN step for special-index "
    "packages, or --extra-index-url.\n"
    "  * Prefer small, CPU-friendly libraries over heavy frameworks when they can do the job "
    "(faster builds, fewer failures).\n"
    "  * Pin versions only when you have a concrete reason.\n\n"
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
        "resources": {
            "type": "object",
            "properties": {
                "memory_mb": {"type": "integer"},
                "cpus": {"type": "number"},
                "timeout_seconds": {"type": "integer"},
            },
        },
        "requires_credential": {"type": ["string", "null"]},
        "credential_help": {"type": ["string", "null"]},
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
            prev = previous if isinstance(previous, dict) else {"program": previous or ""}
            task += (
                "\n\nYour PREVIOUS attempt FAILED.\n"
                f"--- Previous Dockerfile ---\n{(prev.get('dockerfile') or '(none)')[:1500]}\n"
                f"--- Previous program ---\n{(prev.get('program') or '')[:4000]}\n"
                f"--- Failure ---\n{error[:2500]}\n"
                "Diagnose the failure from the error above and submit a corrected tool. "
                "Change only what the error implicates — do not rewrite parts that worked."
            )

        await trace_query(tracer, f"developer:{need_key}", task, options)
        return dict(captured) if captured.get("program") and captured.get("dockerfile") else None

    return developer
