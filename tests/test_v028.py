"""Discovery-audit remediation (v0.28.0): defects found by reading the engine
against its own 107-run corpus rather than by a failing run.

Each test below pins an invariant the audit found stated but not enforced.
"""
from __future__ import annotations

import asyncio

from brain import acquisition, config, evidence
from brain.acquisition import generalize_description
from brain.memory.blackboard import Blackboard
from brain.models import Capability, StageSpec
from brain.observability import NullTracer
from brain.orchestrator import Orchestrator
from brain.planner import StubPlanner


class RecordingTracer(NullTracer):
    """Minimal recorder: the kinds of events the run emitted."""

    def __init__(self):
        self._kinds: list[str] = []

    def event(self, kind, name, status="info", **data):
        self._kinds.append(kind)

    def kinds(self):
        return list(self._kinds)


def _orch(tmp_path, runner, planner, session, tracer=None):
    bb = Blackboard(tmp_path / f"{session}.db", session)
    orch = Orchestrator(session=session, manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner, tracer=tracer)
    return orch, bb


def _prior_run(tmp_path, run_id="unrelated-run"):
    prior = tmp_path / "runs" / run_id
    (prior / "outputs").mkdir(parents=True)
    (prior / "workspace").mkdir()
    # ANSWER.md is the agent's NARRATION envelope — never evidence.
    (prior / "outputs" / "ANSWER.md").write_text("I concluded the widget is blue.\n")
    return prior


# --- an open standing goal is not a session continuation -------------------------- #
def test_open_goal_context_does_not_seed_an_unrelated_run_as_evidence(
        tmp_path, monkeypatch):
    """The audit's sharpest finding. `session_context` carries BOTH the
    previous turn of this session AND the open-standing-goals block. Deriving
    "this run continues a session" from that block being non-empty meant one
    open goal made every later run — in any session — seed whatever run
    keyword-matching picked, and record its ANSWER.md (narration) as a
    DESIGNATED source, i.e. target-tier evidence about a target it never
    observed. `agent prose -> NEVER in the ledger` (ARCHITECTURE.md §6)."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    _prior_run(tmp_path)
    tracer = RecordingTracer()

    async def runner(_t, name, prompt, options):
        return "done"

    planner = StubPlanner([StageSpec(id="work", pattern="single", role="E",
                                     goal="produce the thing")])
    orch, bb = _orch(tmp_path, runner, planner, "goalrun", tracer)
    try:
        asyncio.run(orch.run(
            "measure the throughput and report it",
            seed_run="unrelated-run",
            # Exactly what session.py builds when a goal is open on a FRESH
            # session: goals block present, no previous turn.
            session_context="STANDING GOALS (open — ...):\n- goal #1: throughput",
            continues_session=False))
    finally:
        bb.close()
    ws = tmp_path / "runs" / "goalrun" / "workspace"
    assert not (ws / "prior" / "unrelated-run" / "outputs" / "ANSWER.md").exists()
    assert "revision.deliverable_seeded" not in tracer.kinds()
    # And nothing was designated: the ledger holds no laundered narration.
    ledger = evidence.ledger(tmp_path / "runs" / "goalrun", [], [])
    assert not [e for e in ledger if "unrelated-run" in str(e.file)]


def test_a_real_session_turn_still_seeds_the_prior_deliverable(tmp_path, monkeypatch):
    """The complement: continuity stated as a FACT still seeds, so the fix
    removes the false trigger without weakening the real one."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    prior = _prior_run(tmp_path, "prev-run")
    (prior / "outputs" / "DELIVERABLE.md").write_text("### Q: one\nanswer\n")
    tracer = RecordingTracer()

    async def runner(_t, name, prompt, options):
        return "revised"

    planner = StubPlanner([StageSpec(id="revise", pattern="single", role="E",
                                     goal="update the affected answers")])
    orch, bb = _orch(tmp_path, runner, planner, "sessrun", tracer)
    try:
        asyncio.run(orch.run("the details changed; update it",
                             seed_run="prev-run",
                             session_context="previous run id: prev-run",
                             continues_session=True))
    finally:
        bb.close()
    ws = tmp_path / "runs" / "sessrun" / "workspace"
    assert (ws / "prior" / "prev-run" / "outputs" / "DELIVERABLE.md").is_file()
    assert "revision.deliverable_seeded" in tracer.kinds()


# --- the fabrication battery is the same at every trust boundary ------------------ #
class _FabricatingRes:
    """Substantive output from nothing / from unparseable input."""
    ok, oom, exit_code, timed_out = True, False, 0, False
    stderr = ""
    stdout = '{"findings": "' + "x" * 500 + '"}'
    output_files: dict = {}


class _HonestWithoutInputRes:
    """Declines honestly when its preconditions are unmet — passes no-input."""
    ok, oom, exit_code, timed_out = False, False, 2, False
    stderr = ""
    stdout = '{"error": "no input provided"}'
    output_files: dict = {}


class _PassesNoInputFabricatesOnJunkMgr:
    """The exact shape the audit found in the wild: a tool that survives the
    probe promotion DID run, and fails the one promotion skipped."""

    def run_ephemeral(self, image, command, *, policy, capability,
                      environment=None, input_files=None,
                      capture_output_files=False):
        return _FabricatingRes() if input_files else _HonestWithoutInputRes()


def _file_consumer() -> Capability:
    return Capability(
        name="reader", description="reads a provided file", image_tag="x",
        command=["r"], env_map={},
        input_schema={"type": "object", "properties": {"files": {"type": "array"}}},
        emits="observation")


def test_promotion_runs_the_battery_revalidation_will_run():
    """The battery a capability must pass to STAY trusted is the battery it
    must pass to BECOME trusted. Promotion ran only the no-input probe, so a
    tool could be stamped with the CURRENT validation epoch having never faced
    the irrelevant-input probe — then fail it on first reuse and be quarantined
    mid-run (observed: a promoted tool carrying stamp 0.21.0 quarantined by
    'IRRELEVANT-INPUT PROBE FAILED'). Detection was deferred, not avoided."""
    cap = _file_consumer()
    mgr = _PassesNoInputFabricatesOnJunkMgr()
    # This tool SURVIVES the probe promotion already ran...
    ok, _ = acquisition._no_input_probe(cap, {"sample_files": {}}, mgr, None)
    assert ok, "precondition: the tool must pass the probe promotion already ran"
    # ...and fails the one it skipped, which revalidation would later run.
    ok, reason = acquisition._irrelevant_input_probe(cap, mgr, None)
    assert not ok
    # The shared battery therefore rejects it at EVERY trust boundary.
    ok, reason = acquisition._fabrication_probes(cap, {"sample_files": {}}, mgr)
    assert not ok and "IRRELEVANT-INPUT" in reason


def test_every_trust_boundary_routes_through_the_shared_battery():
    """Structural: the probes are reachable ONLY via `_fabrication_probes`, so
    a future trust boundary cannot quietly run half the battery again.

    Counts BOTH reference forms — `f(...)` and the bare `f,` handed to
    `asyncio.to_thread` — because every call site in this module uses the
    second, and a check that missed it would not detect the regression it
    exists to prevent."""
    text = open(acquisition.__file__, encoding="utf-8").read()

    def refs(name: str) -> int:
        return text.count(f"{name}(") + text.count(f"{name},")

    # Each probe: its own `def` line, plus exactly one use inside the battery.
    assert refs("_no_input_probe") == 2, "a probe is invoked outside the battery"
    assert refs("_irrelevant_input_probe") == 2, "a probe is invoked outside the battery"
    # And the battery itself is wired at all three boundaries:
    # authored promotion, recipe promotion, revalidation (+ its own def).
    assert refs("_fabrication_probes") == 4


# --- description generalization must not corrupt the catalog ---------------------- #
def test_generalize_strips_paths_but_never_english_words():
    """`_PATH_RESIDUE` matched the BARE words input/inputs/prior/workspace, so
    the function that exists to clean the planner's reuse catalog was corrupting
    it: the live registry still holds 'a provided file: query (str)' where the
    tool wrote 'Input: query (str)'."""
    assert generalize_description(
        "Input: query (str), optional max_results (int).") == \
        "Input: query (str), optional max_results (int)."
    assert "input files" in generalize_description(
        "Given a Python code string and any paths to local input files it needs.")
    assert "prior analysis" in generalize_description("Reads the prior analysis.")
    assert "workspace summary" in generalize_description("Writes the workspace summary.")
    # Real paths are still stripped.
    out = generalize_description("Parse input/pack.zip and prior/run-a/outputs/X.md.")
    assert "input/pack.zip" not in out and "prior/run-a" not in out
    assert "a provided file" in out


def test_generalize_preserves_sentence_punctuation_and_locators():
    """Two more corruptions in the same function: a greedy path match ate the
    full stop that ended a sentence, and substituting filenames BEFORE locators
    tore a URL in half."""
    # A leading-dot extension keeps its preceding space.
    assert generalize_description(
        "Given a path to a local .aslog export file.").endswith("local .aslog export file.")
    # A path ending a sentence leaves the full stop behind.
    assert generalize_description("Reads /work/in/a.csv.").endswith(".")
    # A URL is replaced whole, not shredded.
    out = generalize_description("Fetch https://example.com/data.csv and parse it.")
    assert out == "Fetch a provided source and parse it."


# --- an oversized image is FITTED, not discarded ---------------------------------- #
def _noise_png(w=1600, h=1000):
    """A real, poorly-compressible PNG — genuinely large on disk."""
    import io
    import os

    from PIL import Image
    img = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_an_oversized_image_is_downscaled_rather_than_dropped(tmp_path):
    """`max_bytes` bounds what may be SENT, not what may be READ. The raw-size
    guard sat two lines above the downscaler that exists to make exactly such a
    file fit, so a rendered page or extracted frame the tool produced and
    retained never reached the agent — and a review that demands perception
    then voids its own approval as "unperceived artifact", shipping the run
    unverified for want of evidence that existed all along."""
    from brain.capabilities.runtime import image_blocks

    ws = tmp_path / "ws"
    (ws / "cap").mkdir(parents=True)
    data = _noise_png()
    (ws / "cap" / "page1.png").write_bytes(data)
    per_image_cap = len(data) // 4                     # the file is 4x the cap

    blocks = image_blocks(["cap/page1.png"], ws, max_images=3,
                          max_bytes=per_image_cap, budget_bytes=2_000_000)

    assert blocks, "an oversized but decodable image must be fitted, not dropped"
    assert blocks[0]["type"] == "image"
    # Fitted down to the encoded ceiling the caller asked for.
    assert len(blocks[0]["data"]) <= _encoded_ceiling(per_image_cap)


def _encoded_ceiling(raw_bytes: int) -> int:
    return (raw_bytes * 4 + 2) // 3


def test_undecodable_files_are_still_skipped(tmp_path):
    """The guard that remains: bytes that are not an image at all."""
    from brain.capabilities.runtime import image_blocks

    ws = tmp_path / "ws"
    (ws / "cap").mkdir(parents=True)
    (ws / "cap" / "broken.png").write_bytes(b"x" * 4096)
    (ws / "cap" / "empty.png").write_bytes(b"")
    assert image_blocks(["cap/broken.png", "cap/empty.png"], ws,
                        max_images=3, max_bytes=1024) == []
