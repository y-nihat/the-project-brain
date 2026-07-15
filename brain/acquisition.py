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

from . import config, factory
from .models import Capability
from .observability import NullTracer, trace_query
from .sandbox import policy
from .verifier import VerificationResult, verify

CAP_IMAGE_PREFIX = "brain/cap-"


class AcquisitionError(RuntimeError):
    """A capability could not be acquired. Carries CLASSIFIED failure state so
    the controller can treat the failure as an observation about the method —
    route it to strategy adjudication — instead of a bare crash (the observed
    terminal failure: three authoring dead ends escaped as a raw traceback and
    no alternative strategy was ever considered).

    ``failure_class``: authority (credential/approval denied), packaging (image
    build / dependency), implementation (built but failed operational
    verification), contract (violated the evidence/authoring contract),
    submission (developer produced nothing usable), suppressed (method already
    failed this run and no new rationale was given)."""

    def __init__(self, message: str, *, need: str = "", failure_class: str = "unknown",
                 attempts: int = 0, history: Optional[list[str]] = None):
        super().__init__(message)
        self.need = need
        self.failure_class = failure_class
        self.attempts = attempts
        self.history = list(history or [])


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
    # Evidence contract, same as every observer: retain the raw payload under
    # a UNIQUE name (repeated searches must never overwrite earlier evidence),
    # report what was ACTUALLY accessed (the search index, never the linked
    # pages), and report yield + observation state honestly — snippets are
    # index excerpts, so coverage of any deeper property is at most partial.
    import time
    os.makedirs("/work/out", exist_ok=True)
    payload_name = "search-results-%d.json" % int(time.time() * 1000)
    with open("/work/out/" + payload_name, "w") as fh:
        json.dump({"query": query, "results": results}, fh, indent=2)
    print(json.dumps({
        "query": query,
        "results": results,
        "payload_file": payload_name,
        "source_refs": ["search-index://duckduckgo/?q=" + query],
        "coverage": "partial" if results else "none",
        "observation_state": "observed_present" if results else "not_detected",
        "unresolved_needs": (
            ["full content of the result pages (snippets are search-index "
             "excerpts; the linked pages were NOT fetched)"] if results
            else ["any results for this query via this index"]),
        "consumed_inputs": [],
    }))
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
        # Discovery-class observation: snippets are retained evidence of what
        # the index returned, NOT of the linked pages' full content.
        emits="observation",
    )


def web_search_build_context() -> dict[str, str]:
    return {"Dockerfile": WEB_SEARCH_DOCKERFILE, "search.py": WEB_SEARCH_PROGRAM}


# (make_capability, make_build_context, free_hint, sample_input) — the sample
# input drives the SAME generic evidence-contract verification every authored
# observer passes; recipes get no privileged bypass.
_RECIPES: dict[str, tuple] = {
    "web_search": (web_search_capability, web_search_build_context,
                   "ddgs (DuckDuckGo, no API key)",
                   {"query": "site reliability engineering", "max_results": 3}),
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
    example_input: Optional[dict] = None,
    research_fn=None,
    workspace=None,
    failure_log: Optional[list] = None,
) -> Capability:
    """Acquire the capability for ``need_key`` (recipe if known, else author it).

    ``pre_interaction`` (async str -> None) is consulted before any user-facing
    ask, so the supervisor can equip the user first. ``example_input`` carries
    the CONCRETE instance values (a locator, a query) for verification only —
    the description stays an instance-neutral contract. ``research_fn`` gives the
    developer agent a channel to request research when it lacks knowledge.
    ``failure_log`` is the caller's RUN-scoped, cross-contract failure memory:
    entries from earlier acquisitions this run are shown to the developer, and
    this acquisition appends its own failures — per-name memory alone lets an
    identical approach fail again under every new name a replan invents."""
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
        pre_interaction=pre_interaction, example_input=example_input, research_fn=research_fn,
        workspace=workspace, failure_log=failure_log,
    )


# --------------------------------------------------------------------------- #
# Recipe path (bootstrap)
# --------------------------------------------------------------------------- #
async def _acquire_from_recipe(
    need_key, recipe, *, manager, registry, builder, max_attempts, tracer
) -> Capability:
    make_cap, make_context, free_hint, sample_input = recipe
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
                # ONE evidence contract for every observer (V060 finding: the
                # bootstrap path was exempt from the invariant applied to
                # authored tools — the most trusted tool had the weakest
                # contract). Generic verification (incl. evidence-contract
                # checks) runs FIRST; the recipe's shape verifier remains as
                # an extra known-good check.
                ok, detail, _ = await asyncio.to_thread(
                    _generic_verify, cap, sample_input or {}, {}, manager)
                if ok:
                    result: VerificationResult = await asyncio.to_thread(verify, cap, manager)
                else:
                    result = VerificationResult(False, f"evidence contract: {detail}")
                vsp.set(passed=result.passed, detail=result.detail, source=source)
                if not result.passed:
                    vsp.status = "failed"

            if result.passed:
                cap.verified = True
                cap.validated_engine = validation_epoch()
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

        raise AcquisitionError(f"could not build a working '{need_key}': {last_detail}",
                               need=need_key, failure_class="implementation",
                               attempts=max_attempts)


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
        accepts=[str(a) for a in (sub.get("accepts") or [])],
        # Undeclared output role defaults to "derived" — fail-CLOSED for
        # evidence: derived output is never treated as original source.
        emits=str(sub.get("emits") or "derived").strip().lower(),
    )


_PATH_RESIDUE = re.compile(r"\b(?:input|workspace|prior|/work)[\w./-]*", re.IGNORECASE)
_FILENAME_RESIDUE = re.compile(r"\b[\w-]+\.(?:pdf|md|txt|csv|json|html?|docx?|xlsx?|png|jpe?g)\b",
                               re.IGNORECASE)
# Remote locators are task residue exactly like local paths: a promoted tool
# whose description names the originating URL becomes an availability anchor
# for every future objective that mentions that source.
_URL_RESIDUE = re.compile(r"(?:\w{2,10}://|www\.)\S+", re.IGNORECASE)
# Output-shape POLICY belongs to the task that asked for it, never to the
# reusable mechanism ("write a single paragraph (maximum 10 sentences)" was
# observed promoted as a tool's global identity).
_COUNT_POLICY_RESIDUE = re.compile(
    r"\(?\b(?:exactly|no more than|at most|maximum(?: of)?|up to|a single|one)\s+"
    r"(?:\w{1,12}\s+)?(?:sentence|paragraph|word|page|slide|bullet|item|line)s?\b\)?",
    re.IGNORECASE)


def generalize_description(desc: str) -> str:
    """Consolidation hygiene (#15): a promoted capability is reusable procedural
    memory, so its description must be a GENERAL contract, not the originating
    task. Strip task residue — local paths/filenames, remote locators, and
    output-shape policy — that would mislead or anchor future planning."""
    d = _PATH_RESIDUE.sub("a provided file", desc or "")
    d = _FILENAME_RESIDUE.sub("a provided file", d)
    d = _URL_RESIDUE.sub("a provided source", d)
    d = _COUNT_POLICY_RESIDUE.sub("", d)
    d = re.sub(r"\(\s*\)", "", d)              # empty parens left by stripping
    d = re.sub(r"\s+([,.;:])", r"\1", d)
    d = re.sub(r"\s{2,}", " ", d).strip()
    return d[:500]


def _declares_inputs(cap: Capability, sub: dict) -> bool:
    """True when the tool's contract says it consumes something: env-mapped
    arguments, a `files` argument, or sample files used to verify it."""
    schema_props = (cap.input_schema or {}).get("properties") or {}
    return bool(cap.env_map) or "files" in schema_props or bool(sub.get("sample_files"))


# A no-input result longer than this (or any produced file) is substantive
# content invented from nothing — the fabrication signature. Below it, an
# empty/degenerate result (e.g. {"echo": ""}) is honest, not fabricated.
_FABRICATION_MIN_CHARS = 400


def _no_input_probe(cap: Capability, sub: dict, manager, extra_env: Optional[dict]):
    """Run the tool with NO inputs. For a tool that declares inputs, producing
    SUBSTANTIVE content (a large payload or output files) from nothing is the
    fabrication signature (generic: readers, API clients, DB tools). An empty
    or error result is honest and passes. Pure generators (no declared inputs)
    are exempt — producing from nothing IS their contract.

    Biased to precision: a false positive would block a legitimate tool and
    stall the self-authoring loop, so we only flag clear substantive
    fabrication; the developer contract, provenance labels, and review stage
    are the backstops for subtler cases."""
    if not _declares_inputs(cap, sub):
        return True, "ok (tool declares no inputs; probe not applicable)"
    pol = policy.for_capability(cap.memory_mb, cap.cpus, cap.timeout_seconds, cap.network)
    res = manager.run_ephemeral(
        cap.image_tag, cap.command, policy=pol, capability=cap.name,
        environment=dict(extra_env or {}), input_files=None, capture_output_files=True,
    )
    out = (res.stdout or "").strip()
    payload_error = False
    if out:
        try:
            data = json.loads(out.splitlines()[-1])
            payload_error = isinstance(data, dict) and "error" in data
        except Exception:  # noqa: BLE001
            pass
    substantive = bool(getattr(res, "output_files", None)) or len(out) >= _FABRICATION_MIN_CHARS
    if res.ok and not payload_error and substantive:
        return (False,
                "FABRICATION PROBE FAILED: the tool declares inputs but produced SUBSTANTIVE "
                "content (large payload or output files) when given NONE — it invents substitute "
                'evidence. It must return {"error": "..."} and exit non-zero when its input '
                f"preconditions are unmet. Probe stdout head: {out[:200]!r}")
    return True, "ok (no substantive content fabricated without input)"


def _irrelevant_input_probe(cap: Capability, manager, extra_env: Optional[dict]):
    """Feed a file-consuming tool an irrelevant text file. Substantive output
    from content the tool cannot possibly have parsed = fabrication."""
    if "files" not in ((cap.input_schema or {}).get("properties") or {}):
        return True, "ok (tool does not consume files; probe not applicable)"
    pol = policy.for_capability(cap.memory_mb, cap.cpus, cap.timeout_seconds, cap.network)
    res = manager.run_ephemeral(
        cap.image_tag, cap.command, policy=pol, capability=cap.name,
        environment=dict(extra_env or {}),
        input_files={"__probe__.txt": b"engine probe: deliberately irrelevant input"},
        capture_output_files=True,
    )
    out = (res.stdout or "").strip()
    payload_error = False
    if out:
        try:
            payload_error = isinstance(json.loads(out.splitlines()[-1]), dict) and \
                "error" in json.loads(out.splitlines()[-1])
        except Exception:  # noqa: BLE001
            pass
    substantive = bool(getattr(res, "output_files", None)) or len(out) >= _FABRICATION_MIN_CHARS
    if res.ok and not payload_error and substantive:
        return (False, "IRRELEVANT-INPUT PROBE FAILED: substantive output from an input the "
                       f"tool cannot have parsed — it substitutes evidence. Head: {out[:200]!r}")
    return True, "ok (irrelevant input handled honestly)"


_STRUCTURAL_MARKUP_TAGS = ("html", "body", "section", "table", "div")


def _markup_integrity_check(res) -> tuple[bool, str]:
    """Gross-malformation gate for produced markup files: a generator whose
    sample run already emits truncated or structurally broken HTML ships
    defects into every consuming run (observed three cycles running — the
    same cached report generator produced malformed output each time, and
    every run burned repair/review budget downstream on defects verification
    could have refused at birth). Deliberately coarse, only unarguable
    defects fail: a file ending inside an unterminated tag, an unclosed
    <script>/<style> block, or a structural open/close imbalance >= 2."""
    for name, data in (getattr(res, "output_files", None) or {}).items():
        if not str(name).lower().endswith((".html", ".htm")):
            continue
        try:
            text = bytes(data).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — undecodable output is not this gate's call
            continue
        problems = []
        cut = text.rfind("<")
        if cut >= 0 and ">" not in text[cut:]:
            problems.append("the file ends inside an unterminated tag (truncated write)")
        for tag in ("script", "style"):
            if len(re.findall(rf"(?i)<{tag}\b", text)) > \
                    len(re.findall(rf"(?i)</{tag}\s*>", text)):
                problems.append(f"an unclosed <{tag}> block")
        for tag in _STRUCTURAL_MARKUP_TAGS:
            opens = len(re.findall(rf"(?i)<{tag}\b[^>]*(?<!/)>", text))
            closes = len(re.findall(rf"(?i)</{tag}\s*>", text))
            if abs(opens - closes) >= 2:
                problems.append(f"<{tag}> open/close imbalance ({opens} open vs "
                                f"{closes} close)")
        if problems:
            return (False,
                    f"MARKUP INTEGRITY: produced file {name!r} is malformed — "
                    + "; ".join(problems)
                    + ". Write the COMPLETE document in one piece and close every "
                      "element you open, then re-submit.")
    return True, "ok"


# Bump when the probe battery itself changes behavior (the developer contract
# is fingerprinted automatically; the probes are code, so they need a manual epoch).
# 3: evidence-contract checks (contradictory yield, observation-role yield fields,
#    remote lineage + retention) joined verification.
# 4: observation_state required for observation-role tools + state/coverage
#    contradiction checks (absence and failure must never share an output shape).
# 5: markup-integrity gate on produced .html output joined verification.
_PROBE_SUITE_VERSION = "5"


def validation_epoch() -> str:
    """The validation stamp a trusted capability must carry: engine version PLUS
    a fingerprint of the authoring contract and probe suite. A contract change
    (new rules for readers/writers) invalidates previously promoted capabilities
    even when the release label does not move — the observed failure was a
    biased reader staying 'validated' under a stale ENGINE_VERSION while newly
    authored tools got the corrected contract."""
    import hashlib
    h = hashlib.sha256((_DEVELOPER_SYSTEM + _PROBE_SUITE_VERSION).encode()).hexdigest()[:10]
    return f"{config.ENGINE_VERSION}+c{h}"


def revalidate_capability(cap: Capability, manager, registry) -> tuple[bool, str]:
    """Procedural-memory immune check (#9): run the CURRENT probe battery
    against an already-promoted capability before it is reused. Rebuilds the
    image from the stored build context if it is missing. Returns (ok, reason).

    A capability whose declared contract predates the current one is STALE by
    construction — probes cannot verify semantics the contract never asked it
    to declare, so it must be re-authored, not re-stamped. This covers EVERY
    evidence-producing class: file consumers (accepts/files) AND remote
    observers (network access) — two network observers were re-stamped under
    a new epoch while declaring no output role, no source_refs, and retaining
    no payloads, so the whole evidence plane failed open for their class."""
    observes = (cap.accepts
                or "files" in ((cap.input_schema or {}).get("properties") or {})
                or (cap.network or "none") != "none")
    if not cap.emits and observes:
        return False, ("authored under an older capability contract: it observes "
                       "(consumes files or reaches external sources) but declares no "
                       "`emits` output role, so its output cannot be routed as "
                       "evidence — re-author under the current contract")
    try:
        try:
            manager.ensure_image(cap.image_tag)
        except Exception:  # noqa: BLE001 — image gone: rebuild from stored context
            bc_dir = Path(registry.store_dir) / cap.name / "build"
            if not bc_dir.is_dir():
                return False, "image missing and no stored build context to rebuild from"
            manager.build_image(cap.image_tag,
                                {p.name: p.read_text() for p in bc_dir.iterdir() if p.is_file()})
        sub_like = {"sample_files": {}}
        ok, reason = _no_input_probe(cap, sub_like, manager, None)
        if not ok:
            return False, reason
        ok, reason = _irrelevant_input_probe(cap, manager, None)
        if not ok:
            return False, reason
        return True, "passed current probe battery"
    except Exception as e:  # noqa: BLE001 — cannot validate => not safe to reuse
        return False, f"revalidation errored: {type(e).__name__}: {str(e)[:200]}"


def _generic_verify(cap: Capability, sample_input: dict, sample_files: dict, manager,
                    extra_env: Optional[dict] = None, workspace=None):
    """Generic acceptance: run the tool on a sample input (plus optional sample
    files placed under /work/in); success = exit 0 and a parseable, non-error JSON
    object on the last stdout line. Returns (ok, detail, run_result) — detail
    carries the TAIL of stderr (the end is where tracebacks live).

    ``extra_env`` carries session secrets (credential env vars) so a
    credentialed tool can be verified for real; values are never traced.

    ``workspace`` enables REAL-INPUT verification: `sample_input.files`
    (workspace-relative paths — the actual files this run holds) are mounted
    under /work/in at their basename, exactly as the runtime does. Without
    this, a tool whose genuine input is a real/binary file (an archive, a
    spreadsheet, a recording, a database dump) could never see its input at
    verification — the file references used to be silently DISCARDED here, so
    every such tool failed 'file not found' three times regardless of code
    quality, and the run died on a healthy input sitting in the workspace."""
    args = dict(sample_input or {})
    file_refs = args.pop("files", None)   # workspace paths to mount, not an env input
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
    if file_refs:
        from pathlib import Path as _Path
        # A ref whose basename a synthetic fixture already covers is satisfied
        # (developers echo the schema's `files` arg alongside sample_files).
        unresolved = [r for r in file_refs
                      if _Path(str(r)).name not in input_files]
        if unresolved and workspace is None:
            return (False,
                    "SAMPLE FILES UNAVAILABLE: sample_input.files references run files, "
                    "but no workspace is attached to this verification. Use sample_files "
                    "(synthetic text fixtures) instead, or drop the references.", None)
        root = _Path(workspace).resolve() if workspace is not None else None
        for ref in unresolved:
            p = (root / str(ref)).resolve()
            if not str(p).startswith(str(root)) or not p.is_file():
                # List what actually exists (parity with the runtime's
                # file-not-found error): a developer was observed guessing
                # paths three times while the real file sat one listing away.
                avail = [str(x.relative_to(root)) for x in sorted(root.rglob("*"))
                         if x.is_file() and x.relative_to(root).parts[:1] != ("prior",)]
                shown = avail[:40]
                more = f" (showing first 40 of {len(avail)})" if len(avail) > 40 else ""
                return (False,
                        f"SAMPLE FILE NOT FOUND: sample_input.files references {str(ref)!r}, "
                        "which does not exist in the run workspace — reference a REAL "
                        f"file exactly as listed. Files that exist{more}: {shown}", None)
            try:
                input_files[p.name] = p.read_bytes()
            except OSError as e:
                return (False, f"SAMPLE FILE UNREADABLE: {str(ref)!r}: {e}", None)
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
    # Observation-reporting contract (#10): a file-consuming tool must state
    # what it actually read, or downstream provenance is unverifiable.
    if input_files and isinstance(data, dict) and "consumed_inputs" not in data:
        return (False, "output JSON must include `consumed_inputs`: the list of input "
                       "filenames the tool actually read (empty list if none). Provenance "
                       "depends on this being reported truthfully.", res)
    ok, detail = _evidence_contract_check(cap, data if isinstance(data, dict) else {}, res)
    if not ok:
        return (False, detail, res)
    ok, detail = _markup_integrity_check(res)
    if not ok:
        return (False, detail, res)
    return (True, f"ok: {str(data)[:120]}", res)


def _evidence_contract_check(cap: Capability, data: dict, res) -> tuple[bool, str]:
    """Deterministic semantic-lineage checks on a verified run's output (#P0-C/D).

    Operational success and instance yield are ORTHOGONAL: exit-0 with
    coverage='none' is a valid observation ("nothing of the requested property
    exists via this channel"), never a tool failure. What IS rejected:

      * coverage that contradicts the reported unresolved needs — 'complete'
        cannot coexist with named unobserved properties (length-based or
        substituted coverage shows up exactly here);
      * an observation-role tool that reports no yield fields at all — its
        output cannot be routed as evidence;
      * a remote observer claiming positive yield while retaining NO raw
        payload — an unretained observation cannot support any claim later.
    """
    coverage = str(data.get("coverage", "")).strip().lower()
    needs = data.get("unresolved_needs")
    needs_list = needs if isinstance(needs, list) else None
    if coverage == "complete" and needs_list:
        return (False,
                "CONTRADICTORY YIELD: the output claims coverage='complete' while also "
                f"naming unresolved needs {needs_list[:3]}. Coverage is measured against the "
                "REQUESTED property only — if any part of it could not be observed, coverage "
                "is 'partial' or 'none'. A fallback/adjacent channel never inherits the "
                "requested property's coverage.")
    if cap.emits == "observation":
        if "coverage" not in data or "unresolved_needs" not in data:
            return (False,
                    "MISSING EVIDENTIARY YIELD: an observation-role tool must report "
                    "`coverage` ('complete'|'partial'|'none' for the REQUESTED property) and "
                    "`unresolved_needs` (a list; empty if nothing) in its final JSON — "
                    "without them its output cannot be routed as evidence.")
        state = str(data.get("observation_state", "")).strip().lower()
        if state not in ("observed_present", "observed_absent", "not_detected",
                         "ambiguous", "observation_failed"):
            return (False,
                    "MISSING/INVALID OBSERVATION STATE: an observation-role tool must report "
                    "`observation_state` — one of 'observed_present' (the requested property "
                    "was observed), 'observed_absent' (the channel was adequately examined "
                    "and the property is NOT there), 'not_detected' (this method found "
                    "nothing, but its sensitivity/coverage cannot rule the property out), "
                    "'ambiguous', or 'observation_failed' (the observation itself failed). "
                    "Absence and failure-to-observe are DIFFERENT facts and must never share "
                    "an output shape.")
        if state == "observed_present" and coverage == "none":
            return (False,
                    "CONTRADICTORY STATE: observation_state='observed_present' cannot "
                    "coexist with coverage='none' — if the property was observed, some of "
                    "it was covered.")
        if state in ("observed_absent", "not_detected", "observation_failed") and \
                coverage == "complete":
            return (False,
                    "CONTRADICTORY STATE: coverage measures the YIELD of the requested "
                    f"property, so observation_state='{state}' cannot coexist with "
                    "coverage='complete' — an absent/undetected/failed property yields "
                    "coverage 'none'.")
        if (cap.network or "none") != "none":
            refs = data.get("source_refs")
            if not (isinstance(refs, list) and refs) and coverage in ("complete", "partial"):
                return (False,
                        "MISSING SOURCE LINEAGE: a remote observer reporting positive yield "
                        "must include `source_refs` — the locators it ACTUALLY accessed.")
            if coverage in ("complete", "partial") and not getattr(res, "output_files", None):
                return (False,
                        "UNRETAINED OBSERVATION: a remote observer reporting positive yield "
                        "must write the raw retrieved payloads it relied on to /work/out as "
                        "files (verbatim, not only a summary) — downstream verification "
                        "traces claims to that retained evidence.")
    return (True, "ok")


async def _author_capability(
    need_key, description, *, manager, registry, broker, model, developer, max_attempts, tracer,
    seed: Optional[dict] = None, repair_reason: Optional[str] = None, pre_interaction=None,
    example_input: Optional[dict] = None, research_fn=None, workspace=None,
    failure_log: Optional[list] = None,
) -> Capability:
    dev = developer or make_developer_agent(model, tracer, research_fn=research_fn)
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
        # Classified per-attempt record: the controller adjudicates strategy from
        # WHAT KIND of failure this was and whether attempts are still learning
        # anything — not from a bare retry count.
        last_class = "submission"
        history: list[str] = []
        task = (description
                + (f"\n\nConcrete example input for THIS task (use it in `sample_input` so the "
                   f"tool is verified against the real target; NEVER hardcode these values into "
                   f"the program or Dockerfile — they arrive as arguments at run time): "
                   f"{json.dumps(example_input)}" if example_input else ""))

        def _fail(reason: str) -> AcquisitionError:
            if registry is not None and getattr(registry, "record_failure", None):
                try:
                    registry.record_failure(need_key, reason, last_class)
                except Exception:  # noqa: BLE001 — memory must not mask the failure
                    pass
            return AcquisitionError(f"could not {mode} a working '{need_key}': {reason}",
                                    need=need_key, failure_class=last_class,
                                    attempts=len(history) or max_attempts, history=history)

        # Run-scoped memory: earlier failures THIS RUN (any contract) ride into
        # every authoring attempt — the observed alternative is a replan naming
        # an equivalent capability afresh and the new developer repeating the
        # exact approach that just failed, attempt after attempt.
        log = failure_log if failure_log is not None else []

        for attempt in range(1, max_attempts + 1):
            prior_block = ""
            if log:
                prior_block = (
                    "\n\nAPPROACHES THAT ALREADY FAILED THIS RUN (recorded across ALL "
                    "tools/contracts — an equivalent approach under a new name fails "
                    "identically; choose a genuinely DIFFERENT approach, or report the "
                    "intrinsic blocker instead of resubmitting a variant of these):\n"
                    + "\n".join(f"- {str(x)[:220]}" for x in log[-8:]))
            sub = await dev(need_key, task + prior_block, previous, error)
            if not sub or not sub.get("program") or not sub.get("dockerfile"):
                last_detail = "developer produced no usable submission"
                last_class = "submission"
                history.append(f"attempt {attempt}: submission — nothing usable")
                error = last_detail
                continue
            consumes_files = bool(sub.get("accepts")) or \
                "files" in ((sub.get("input_schema") or {}).get("properties") or {})
            observes = consumes_files or sub.get("network") == "bridge"
            if observes and not sub.get("emits"):
                last_detail = "observing tool did not declare its output role"
                last_class = "contract"
                history.append(f"attempt {attempt}: contract — missing `emits` declaration")
                error = ("Your submission is missing `emits`. A tool that OBSERVES (consumes "
                         "files or reaches external sources over the network) MUST declare what "
                         "its output IS: 'observation' (verbatim extraction), 'derived' "
                         "(interpretation/summary), 'artifact' (produced deliverable), or "
                         "'validation' (a check result). Resubmit with `emits` set honestly.")
                previous = {"program": sub["program"], "dockerfile": sub["dockerfile"]}
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
                        last_class = "authority"
                        history.append(f"attempt {attempt}: authority — approval for {rc} denied")
                        raise _fail(f"approval denied: it needs {rc}")
                    secret = broker.collect_secret(
                        rc, purpose=f"capability '{need_key}' ({description[:80]})",
                        help_text=sub.get("credential_help"))
                    if not secret:
                        last_class = "authority"
                        history.append(f"attempt {attempt}: authority — no value for {rc}")
                        raise _fail(f"it needs {rc}, but no value was provided")
                    secret_env[rc] = secret

            cap = _capability_from_submission(need_key, description, sub)
            cap.requires_credential = rc or None
            cap.credential_help = (str(sub.get("credential_help")).strip()[:2000]
                                   if rc and sub.get("credential_help") else None)
            build_context = {"Dockerfile": sub["dockerfile"], "tool.py": sub["program"]}
            attempt_dir = tracer.artifact_path(cap.name, f"attempt-{attempt}-author")
            _write_context(attempt_dir, build_context)

            # Transactional build: an UNVERIFIED candidate must never replace the
            # canonical (possibly trusted) image — all three failed attempts were
            # observed overwriting `:1.0.0` in place. Build attempt-scoped, verify,
            # then promote by ATOMIC retag. (A manager without tagging — test
            # stubs — keeps the legacy in-place behavior.)
            canonical_tag = cap.image_tag
            tagger = getattr(manager, "tag_image", None)
            if tagger is not None:
                import uuid
                cap.image_tag = f"{CAP_IMAGE_PREFIX}{need_key}:cand-{uuid.uuid4().hex[:8]}"

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
                last_class = "packaging"
                history.append(f"attempt {attempt}: packaging — {_error_tail(tail, 160)}")
                log.append(f"'{need_key}' build [packaging]: {_error_tail(tail, 200)}")
                error = f"The docker image BUILD failed. Error (tail of build log):\n{tail}"
                previous = {"program": sub["program"], "dockerfile": sub["dockerfile"]}
                continue

            with tracer.span("capability.verify", cap.name) as vsp:
                ok, detail, run_res = await asyncio.to_thread(
                    _generic_verify, cap, sub.get("sample_input") or {},
                    sub.get("sample_files") or {}, manager, secret_env or None,
                    workspace,
                )
                if ok:
                    # Fabrication probe: a tool that DECLARES inputs but still
                    # "succeeds" with none is inventing evidence (the exact
                    # defect behind the sample_claude.pdf confabulation) — a
                    # consolidation-blocking failure, fed back for repair.
                    ok, detail = await asyncio.to_thread(
                        _no_input_probe, cap, sub, manager, secret_env or None)
                vsp.set(passed=ok, detail=detail, source="author")
                if not ok:
                    vsp.status = "failed"
                    if run_res is not None:
                        (attempt_dir / "verify.stderr").write_text(run_res.stderr or "")
                        (attempt_dir / "verify.stdout").write_text(run_res.stdout or "")

            if ok:
                if tagger is not None:
                    candidate = cap.image_tag
                    await asyncio.to_thread(tagger, candidate, canonical_tag)
                    cap.image_tag = canonical_tag
                    remover = getattr(manager, "remove_image", None)
                    if remover is not None:  # drop the candidate alias, keep the image
                        await asyncio.to_thread(remover, candidate)
                cap.verified = True
                cap.validated_engine = validation_epoch()  # passed the current battery+contract
                cap.description = generalize_description(cap.description)  # #15: general contract
                promoted_dir = tracer.artifact_path(cap.name, "promoted")
                _write_context(promoted_dir, build_context)
                with tracer.span("capability.promote", cap.name) as psp:
                    registry.add(cap, build_context=build_context)
                    psp.set(source="author",
                            code_path=str(Path(registry.store_dir) / cap.name / "build"),
                            artifact_path=str(promoted_dir))
                return cap

            if tagger is not None and getattr(manager, "remove_image", None):
                await asyncio.to_thread(manager.remove_image, cap.image_tag)
            last_detail = detail
            # Contract violations (fabrication, missing lineage, contradictory
            # yield) are a different KIND of failure from a crashing tool — the
            # controller must see which uncertainty is dominant.
            up = (detail or "").upper()
            last_class = ("contract" if any(k in up for k in (
                "FABRICATION", "CONTRADICTORY YIELD", "MISSING EVIDENTIARY",
                "MISSING SOURCE LINEAGE", "UNRETAINED OBSERVATION", "IRRELEVANT-INPUT"))
                else "implementation")
            history.append(f"attempt {attempt}: {last_class} — {_error_tail(detail, 160)}")
            log.append(f"'{need_key}' verify [{last_class}]: {_error_tail(detail, 200)}")
            error = f"The image built, but VERIFICATION of the running tool failed:\n{detail}"
            previous = {"program": sub["program"], "dockerfile": sub["dockerfile"]}

        raise _fail(last_detail)


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
    max_attempts: int = 2, tracer=None, research_fn=None, workspace=None,
) -> Capability:
    """Repair a trusted-but-degraded tool in place: re-author it (seeded with its
    current code + the live failure), re-verify, and re-promote under the same
    image tag so the re-running agent transparently gets the improved tool."""
    tracer = tracer or NullTracer()
    seed = _load_seed(registry, cap)
    repaired = await _author_capability(
        cap.name, cap.description, manager=manager, registry=registry, broker=broker,
        model=model, developer=developer, max_attempts=max_attempts, tracer=tracer,
        seed=seed, repair_reason=reason, research_fn=research_fn, workspace=workspace,
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
    "  * write the raw results payload to /work/out/search-results.json (retained evidence);\n"
    "  * print exactly one JSON object on the final stdout line: "
    "{{\"query\": <str>, \"results\": [{{\"title\": <str>, \"href\": <str>, \"body\": <str>}}], "
    "\"source_refs\": [<the index locator actually queried>], "
    "\"coverage\": \"partial\"|\"none\", \"unresolved_needs\": [<what snippets do NOT cover>], "
    "\"consumed_inputs\": []}} — snippets are index excerpts, so coverage is at most partial "
    "and the linked pages must be listed as unresolved;\n"
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
    "  * Write any OUTPUT files under /work/out/ (create the directory if needed). "
    "Derive each output FILENAME from the input's identity or an explicit argument — "
    "never one fixed constant path, which makes consecutive calls overwrite each other "
    "(the engine dedupes collisions defensively, but distinct outputs deserve distinct "
    "names). A markup/document artifact (HTML or similar) must be written as ONE "
    "complete, well-formed document — close every element you open; truncated or "
    "structurally unbalanced files fail verification.\n"
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
    "  * sample_input: concrete arguments that should make the tool succeed (used to verify it). "
    "If the tool consumes files and the CURRENT TASK provides real ones, include "
    "`files`: a list of their workspace-relative paths exactly as given in your task "
    "(e.g. paths under input/) — the engine mounts those REAL files under /work/in at "
    "their basename during verification, exactly as at run time. This is the ONLY way "
    "to verify against real/binary inputs (archives, spreadsheets, media, databases): "
    "never fabricate a binary fixture, and never assume a file is present unless you "
    "referenced it here.\n"
    "  * sample_files: an optional map {filename: text_content} placed under /work/in/ so a "
    "file-consuming tool can be verified with a small SYNTHETIC TEXT fixture when the "
    "task provides no real file.\n"
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
    "PERSISTENCE TOOLS WRITE VERBATIM (hard rule): a tool whose job is saving/producing a "
    "file must write the CONTENT IT IS GIVEN exactly — no reformatting, no templating, no "
    "truncating/slicing of strings, no generating its own copy from 'notes'. Creative "
    "generation is the AGENT's job: the agent authors the complete final content and passes "
    "it whole. (A note-templating writer was observed slicing strings mid-word into every "
    "artifact it produced — that tool class is banned.)\n"
    "SUPPLIED-QUANTITY PARAMETERIZATION (hard rule): any quantity the task supplies "
    "or could plausibly vary on the next call — a time window, threshold, count, unit, "
    "label set, grouping — MUST be an input_schema argument (with the current task's "
    "value as its example), NEVER a constant baked into the code or into a fixed menu "
    "of computations. A tool that froze one task's 7-day window into its computation "
    "set was uncallable for the next request's 30-day window: same data, same "
    "operation, dead tool.\n"
    "PERCEPTION FIDELITY: for speech-to-text/OCR/extraction tools, when the output will "
    "carry identity-critical tokens (proper names, exact figures, quoted wording), "
    "prefer the higher-accuracy configuration over the fastest one, and MARK "
    "low-confidence tokens in the output rather than emitting a confident guess.\n"
    "PERCEPTION BEFORE INTERPRETATION (extractor/reader tools): output TWO LAYERS — "
    "`raw_observations` (verbatim evidence: the document title, section headings, short "
    "page/section snippets, the most frequent meaningful terms) and, separately, any "
    "`inferences` (themes/summaries you derived). Every theme must be traceable to observed "
    "terms/snippets; NEVER seed output with domain keyword lists the source did not contain "
    "(extract what IS there, not what you expect). Report extraction `limitations` honestly "
    "(unparsed pages, garbled regions). Prefer exposing clean observations over ambitious "
    "summarization — downstream agents do the interpreting.\n"
    "APPLICABILITY + OBSERVATION REPORTING (hard rules): if your tool consumes files, declare "
    "`accepts` — the list of file extensions it can genuinely process (e.g. [\".pdf\"]); the "
    "engine refuses to run it on anything else, so declare honestly and narrowly. Your final "
    "JSON object MUST include `consumed_inputs`: the list of input filenames the tool actually "
    "read to produce this result (empty list if none). If any part of the output is generated "
    "rather than extracted from inputs, include `generated: true`. Downstream provenance depends "
    "on these being truthful.\n"
    "EVIDENTIARY YIELD (hard rule): operational success is NOT evidence. When your tool "
    "retrieves or observes something, the final JSON MUST report its yield honestly: "
    "`coverage` — 'complete' | 'partial' | 'none' (how much of what was asked for was "
    "actually observed), and `unresolved_needs` — a list naming what could NOT be observed "
    "(empty list if nothing). Exit 0 with partial yield is CORRECT behavior — a tool that "
    "quietly substitutes adjacent data (metadata for content, an index for the document) "
    "poisons every downstream conclusion.\n"
    "OBSERVATION STATE (hard rule): the final JSON MUST also report `observation_state`, "
    "one of: 'observed_present' (the requested property WAS observed), 'observed_absent' "
    "(the channel was adequately examined and the property is genuinely NOT there), "
    "'not_detected' (this method found nothing, but its sensitivity or coverage cannot "
    "rule the property out), 'ambiguous', or 'observation_failed' (the observation itself "
    "failed — could not open/parse/reach the input). ABSENCE AND FAILURE ARE DIFFERENT "
    "FACTS: a processing/extraction failure must NEVER produce the same output as genuine "
    "emptiness — never write an empty result file on a failure path; report "
    "observation_failed (or exit non-zero with an error) so downstream reasoning can "
    "never mistake a broken observation for a demonstrated absence.\n"
    "NEGATIVE OBSERVATION IS A VALID RESULT (hard rule): finding that the requested "
    "property does not exist or is not retrievable at the designated target ('the source "
    "exposes no such channel', 'the record has no such field', 'the document has no such "
    "section') is a SUCCESSFUL observation: exit 0, coverage='none', "
    "observation_state='observed_absent' ONLY when your examination genuinely covered the "
    "channel ('not_detected' otherwise), name it in `unresolved_needs`, and report what "
    "WAS observed. CODE THE ZERO-YIELD PATH DEFENSIVELY: any aggregation over a "
    "possibly-empty collection (max/min/first/indexing, dividing by a count) must be "
    "guarded so genuine emptiness reaches this honest exit instead of crashing — a tool "
    "that crashes on 'nothing there' converts the most decisive observation it can make "
    "into a fake malfunction, and it will be retried forever. "
    'Reserve {"error": ...} + non-zero exit for the tool itself failing to operate '
    "(crash, unreachable service, invalid input) — never for an honest empty yield.\n"
    "CHANNEL LINEAGE (hard rule): every output field and file must name the channel "
    "ACTUALLY observed — a description is `description`, metadata is `metadata`; output "
    "names never claim the property that was asked for but not observed. A fallback or "
    "adjacent channel may be reported as useful extra evidence UNDER ITS TRUE NAME, but "
    "it NEVER inherits the requested property's identity, coverage, or confidence: "
    "metadata is not content, a schema is not current state, documentation is not runtime "
    "output, testimony about a thing is not observation of it. `coverage` is measured "
    "against the REQUESTED property only — length or availability of substitute text is "
    "NOT coverage.\n"
    "REMOTE OBSERVATION (hard rules): a tool observing remote sources (URLs, APIs, remote "
    "services/objects) MUST include `source_refs` — the locators/identifiers it ACTUALLY "
    "accessed — in its final JSON, and MUST write the raw retrieved content it relied on to "
    "/work/out as files (verbatim payloads, not only your summary of them): downstream "
    "verification traces claims to THAT retained evidence, and an unretained observation "
    "cannot support a claim later. If your tool may be invoked repeatedly in one run, write "
    "retained payloads to UNIQUE filenames (a counter or timestamp component) — a later "
    "observation must never overwrite earlier retained evidence. In your final JSON, refer "
    "to output files by BARE filename (they are delivered to the shared workspace under "
    "<tool-name>/<filename>; sandbox-local paths like /work/out/... are meaningless to "
    "anyone downstream).\n"
    "OUTPUT ROLE (hard rule): declare `emits` — what your tool's output IS, epistemically: "
    "'observation' (the output contains the VERBATIM content/structure extracted from the "
    "given inputs — this is the right role even when separate `inferences` are included "
    "alongside the raw layer), 'derived' (ONLY when the output contains no verbatim extraction "
    "at all: pure interpretation/summary/synthesis), 'artifact' (a produced deliverable file), "
    "or 'validation' (a check/verdict about something else). The engine routes evidence by this "
    "declaration — ONLY 'observation' output is treated as original source evidence, so an "
    "extractor that under-declares as 'derived' silently removes the run's ground truth. A "
    "file-consuming tool MUST declare this explicitly.\n"
    "EVIDENCE BOUNDARY (hard rule): a tool must NEVER fabricate, mock, or substitute evidence "
    "when its input preconditions are not met — no sample/placeholder documents, no synthetic "
    "fallback data, no invented API responses. If the expected input is missing or incompatible, "
    'print {"error": "<what was expected vs received>"} and exit non-zero; the brain will repair, '
    "choose another tool, or replan. A tool that invents its own input poisons every downstream "
    "conclusion. Likewise, when interpolating UNTRUSTED content into structured output (HTML, "
    "SQL, shell, markup), apply contextual escaping — content boundaries are part of correctness.\n"
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
    "(faster builds, fewer failures). Do NOT add a heavyweight interpretation/summarization "
    "dependency to an observer — observing and interpreting are different tools.\n"
    "  * Pin versions only when you have a concrete reason.\n"
    "  * DOWNLOADS MUST FAIL THE BUILD WHEN THEY FAIL (hard rule): a Dockerfile step that "
    "downloads models/corpora/data MUST verify the result — check the downloader's return "
    "value and assert the expected files exist in a RUN step that exits non-zero otherwise "
    "(e.g. `RUN python -c \"...; assert ok\" && test -e /app/resources/<expected-file>`). Never use "
    "quiet/best-effort downloads: a build that 'succeeds' with missing resources crashes at "
    "run time, after the expensive work.\n\n"
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
        "accepts": {"type": "array", "items": {"type": "string"}},
        "emits": {"type": "string",
                  "enum": ["observation", "derived", "artifact", "validation"]},
    },
    "required": ["program", "dockerfile", "command", "input_schema", "env_map", "network", "sample_input"],
}


def make_developer_agent(model: Optional[str] = None, tracer=None,
                         research_fn=None) -> Developer:
    tracer = tracer or NullTracer()

    async def developer(need_key, description, previous=None, error=None) -> Optional[dict]:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        captured: dict = {}

        @tool("submit_tool", "Submit the tool implementation and metadata.", _SUBMIT_TOOL_SCHEMA)
        async def submit_tool(args):
            captured.update(args)
            return {"content": [{"type": "text", "text": "received"}]}

        @tool("progress_update",
              "Announce your current step in one short line (e.g. 'writing the parser', "
              "'preparing the Dockerfile'). Call at milestones — authoring is long and "
              "the user is watching a silent terminal otherwise.",
              {"type": "object", "properties": {"status": {"type": "string"}},
               "required": ["status"]})
        async def progress_update(args):
            tracer.event("agent.progress", f"developer:{need_key}", status="info",
                         note=str(args.get("status", ""))[:200])
            return {"content": [{"type": "text", "text": "noted — continue."}]}

        dev_tools = [submit_tool, progress_update]
        allowed = ["mcp__dev__submit_tool", "mcp__dev__progress_update"]
        research_hint = ""
        if research_fn is not None:
            @tool("request_research",
                  "When you DON'T UNDERSTAND something you need to build the tool — an "
                  "unfamiliar library/API, an opaque build or runtime error, the right way to "
                  "handle a format — ask the orchestrator to research it instead of guessing "
                  "or blindly retrying. A research agent investigates docs and the web and "
                  "returns a findings brief (facts, the concrete fix, source URLs). Use it to "
                  "DIAGNOSE a failure and correct the real cause.",
                  {"type": "object",
                   "properties": {"topic": {"type": "string"},
                                  "why": {"type": "string"},
                                  "specifics": {"type": "string",
                                                "description": "exact error text, versions"}},
                   "required": ["topic"]})
            async def request_research(args):
                tracer.event("team.research", f"developer:{need_key}", status="info",
                             topic=str(args.get("topic", ""))[:200])
                findings = await research_fn(str(args.get("topic", "")),
                                             str(args.get("why", "")),
                                             str(args.get("specifics", "")))
                return {"content": [{"type": "text", "text": findings}]}

            dev_tools.append(request_research)
            allowed.append("mcp__dev__request_research")
            research_hint = (
                "\nIf you hit an unfamiliar library/API or an error you do not fully understand, "
                "call request_research BEFORE retrying — diagnose the real cause, then fix it. "
                "Do not repeat the same failing approach blindly.")

        server = create_sdk_mcp_server(name="dev", version="1.0.0", tools=dev_tools)
        options = factory.locked_session(
            system_prompt=_DEVELOPER_SYSTEM
            + "\nDuring long work, call progress_update at each milestone (approach chosen, "
              "code written, Dockerfile ready, about to submit)." + research_hint,
            mcp_servers={"dev": server},
            allowed_tools=allowed,
            model=model,
            max_turns=8,
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
