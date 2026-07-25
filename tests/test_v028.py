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


# --- the answer must carry the diagnosis the engine already computed -------------- #
def test_the_answer_envelope_states_why_the_run_did_not_certify(tmp_path, monkeypatch):
    """Every unverified run in the corpus opened with the same sentence —
    "its grounding review did not approve the content" — and a pointer to a
    file. The per-criterion table and typed claim support were computed and
    written to outputs/<stage>.md, then discarded from the answer, so two runs
    that failed for completely different reasons were indistinguishable to the
    reader. This reproduces the real shape of run-20260714-214545, where three
    of seven criteria came back `unknown` because a prior artifact was never
    retained — a cause the reader could act on, and never saw."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    orch, bb = _orch(tmp_path, None, StubPlanner([]), "trust")
    try:
        (tmp_path / "runs" / "trust" / "outputs").mkdir(parents=True)
        orch._write_review_record("qa_review", [
            {"round": 1, "verdict": "revise", "criteria": [], "claims": []},
            {"round": 2, "verdict": "needs-evidence",
             "criteria": [
                 {"criterion": "Pricing states EUR 5.99/month", "status": "met",
                  "evidence": "draft line 12"},
                 {"criterion": "The 6 unaffected answers are unchanged",
                  "status": "unknown",
                  "evidence": "no retained prior artifact to diff against"},
             ],
             "claims": [
                 {"claim": "Premium is EUR 5.99/month", "support": "direct",
                  "evidence": "brief.md"},
                 {"claim": "The other answers are untouched", "support": "unsupported",
                  "evidence": ""},
             ]},
        ], "needs-evidence")
        report = orch._trust_report()
    finally:
        bb.close()

    # The reader learns the round, the tally, and the SPECIFIC unmet items.
    assert "qa_review" in report and "round 2 of 2" in report
    assert "1 met" in report and "1 unknown" in report
    assert "The 6 unaffected answers are unchanged" in report
    assert "no retained prior artifact to diff against" in report
    assert "[unsupported] The other answers are untouched" in report
    assert "outputs/qa_review.md" in report
    # Met criteria are not repeated — only what blocks certification.
    assert "Pricing states EUR 5.99/month" not in report


def test_a_measured_count_violation_is_named_not_hidden(tmp_path, monkeypatch):
    """An engine-MEASURED count violation outranks any reviewer verdict, but
    the classifier reports the grounding dimension first, so the measurable,
    fixable fact never reached the reader."""
    from brain.criteria import CountCheck

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    orch, bb = _orch(tmp_path, None, StubPlanner([]), "measured")
    try:
        orch._format_violations = [
            CountCheck(criterion="exactly 5 sections", op="exact", unit="section",
                       bound=5, measured=7, ok=False, gating=True)]
        report = orch._trust_report()
    finally:
        bb.close()
    assert "required exact 5 section(s), measured 7" in report
    assert "outranks any verdict" in report


def test_the_trust_report_never_costs_the_reader_their_answer(tmp_path, monkeypatch):
    """Fail-open: malformed records must degrade to silence, not an exception."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    orch, bb = _orch(tmp_path, None, StubPlanner([]), "broken")
    try:
        assert orch._trust_report() == ""          # nothing recorded yet
        orch._review_summary = {"stage": "r", "final": "x", "record": [{"nope": 1}]}
        orch._trust_report()                        # must not raise
    finally:
        bb.close()


# --- the documented one-shot path is not silent ----------------------------------- #
def test_one_shot_cli_streams_the_run_and_reports_cost(tmp_path, monkeypatch, capsys):
    """`scripts/run-objective.sh` and docs/OVERVIEW.md both drive
    `python -m brain.main`, which attached no listener — so the documented,
    non-interactive path printed nothing for a median of ~6 minutes and a p90
    of ~27. A working system was indistinguishable from a hung one. The
    renderer is pure and already existed for the REPL."""
    from brain import main as main_mod

    class _Summary:
        run_id, status, answer = "run-x", "succeeded", "the answer"
        containers, leaks, capabilities = 0, 0, []

    seen: list = []

    class _Session:
        def add_listener(self, fn):
            seen.append(fn)

        async def run_objective(self, objective, files=None):
            for fn in seen:                       # the run emits events
                fn({"kind": "stage", "name": "survey", "phase": "start",
                    "data": {"pattern": "map"}})
            return _Summary()

        def close(self):
            pass

    monkeypatch.setattr(main_mod, "BrainSession", lambda: _Session())
    monkeypatch.setattr(main_mod, "_history_note", lambda *a: "")
    monkeypatch.setattr(main_mod, "_cost_line", lambda rid: "[cost]       $1.23 · 4.5 min")

    asyncio.run(main_mod._amain("do the thing"))
    out = capsys.readouterr().out
    assert "stage survey [map]" in out, "the run must be visible while it runs"
    assert "[cost]" in out and "$1.23" in out, "the run must state what it cost"

    # --quiet still delivers the answer, without the stream.
    seen.clear()
    asyncio.run(main_mod._amain("do the thing", quiet=True))
    out = capsys.readouterr().out
    assert "the answer" in out
    assert "stage survey" not in out


def test_the_stream_renderer_is_shared_not_duplicated():
    """One renderer, one behaviour, both entry points. It renders TRACE
    records, not terminal state, so it belongs in observability — and the
    one-shot CLI must not import the interactive REPL to use it."""
    from brain.observability.stream import format_event as canonical
    from brain.repl import format_event as via_repl
    from brain.main import format_event as via_main
    assert canonical is via_repl is via_main


def test_cost_line_is_fail_open(tmp_path, monkeypatch):
    """Accounting must never break delivery."""
    from brain import config
    from brain import main as main_mod

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "nonexistent.db")
    assert main_mod._cost_line("run-x") == ""


# --- interrupted runs must still be accounted for --------------------------------- #
def _seed_run(tracer, run_id_holder, cost=1.5, tokens=(100, 20)):
    run = tracer.start_run("do a thing")
    run_id_holder.append(run.run_id)
    tracer.event("agent.finished", "stage", status="ok",
                 cost_usd=cost, tokens_in=tokens[0], tokens_out=tokens[1])
    tracer.event("sandbox.run", "cap", status="ok")
    return run


def test_a_killed_run_keeps_its_accounting(tmp_path):
    """7 of 8 aborted runs in the corpus carry NULL cost, tokens, duration and
    event counts — while 59 to 722 of their events sit on disk. `finish()`
    rolled up; `abort_stale_runs()` (the janitor path a killed process takes)
    only stamped a status. Interrupted runs are the EXPENSIVE tail — the
    operator kills what drags on — so every cost view understated the truth
    exactly where it mattered. Recovering the real corpus finds $68.17, +32%
    on the reported total, and the true worst run is $17.30, not $10.74."""
    from brain.observability.tracer import Tracer

    tracer = Tracer(db_path=tmp_path / "t.db", runs_dir=tmp_path / "runs")
    ids: list = []
    try:
        _seed_run(tracer, ids, cost=2.25, tokens=(500, 40))       # left 'running'
        assert tracer.abort_stale_runs() == 1
        row = tracer._conn.execute(
            "SELECT status, cost_usd, tokens_in, n_events, n_containers, duration_ms, "
            "       finished_at FROM runs WHERE run_id=?", (ids[0],)).fetchone()
    finally:
        tracer.close()

    assert row["status"] == "aborted"
    assert row["cost_usd"] == 2.25, "spend recovered from the events on disk"
    assert row["tokens_in"] == 500
    assert row["n_events"] >= 2 and row["n_containers"] == 1
    assert row["duration_ms"] is not None
    assert row["finished_at"], "finish time comes from the last event, not a later clock"


def test_backfill_repairs_history_and_is_idempotent(tmp_path):
    """The events were always on disk; only the summary was missing."""
    from brain.observability.tracer import Tracer

    tracer = Tracer(db_path=tmp_path / "t.db", runs_dir=tmp_path / "runs")
    ids: list = []
    try:
        _seed_run(tracer, ids, cost=3.5)
        # Simulate the historical shape: finished, but never rolled up.
        tracer._conn.execute(
            "UPDATE runs SET status='aborted', cost_usd=NULL, tokens_in=NULL, "
            "n_events=NULL, duration_ms=NULL WHERE run_id=?", (ids[0],))
        tracer._conn.commit()

        assert tracer.backfill_rollups() == 1
        cost = tracer._conn.execute(
            "SELECT cost_usd FROM runs WHERE run_id=?", (ids[0],)).fetchone()[0]
        assert cost == 3.5
        assert tracer.backfill_rollups() == 0, "idempotent: nothing left to repair"
    finally:
        tracer.close()


def test_stats_reports_the_outcome_mix_and_reuse(tmp_path):
    """Every number here was computable from day one and none was ever asked."""
    from brain.observability.tracer import Tracer
    from brain.observability.view import render_stats

    tracer = Tracer(db_path=tmp_path / "t.db", runs_dir=tmp_path / "runs")
    ids: list = []
    try:
        run = _seed_run(tracer, ids, cost=1.0)
        tracer.event("decision", "resolver", status="info",
                     choice="reuse", capability="write_markdown_file")
        tracer.event("decision", "resolver", status="info", choice="author")
        run.finish("unverified", outcome="not certified")
        text = render_stats(tmp_path / "t.db")
    finally:
        tracer.close()

    assert "unverified" in text and "$1.00" in text
    assert "1 reuse vs 1 author decisions (50% reuse)" in text
    assert "write_markdown_file" in text


# --- count-criteria measurement: coverage, and honesty about coverage ------------- #
def test_a_modifier_between_bound_and_unit_no_longer_hides_a_constraint():
    """The unit had to follow the number immediately, so one adjective dropped
    a real constraint out of deterministic measurement and back into model
    judgment — silently, because an unparsed constraint is indistinguishable
    from an unbounded one. Both of these appear verbatim in the corpus."""
    from brain.criteria import check_counts

    payload = "# T\n\n## A\n\np.\n\n## B\n\np.\n"
    for text in ("The HTML contains exactly 3 distinct pages or full-viewport sections",
                 "Exactly three distinct pages are present in the presentation"):
        checks = check_counts([text], payload)
        assert checks, f"no constraint parsed from {text!r}"
        assert checks[0].bound == 3

    # The window is bounded, so a number and a far-away noun are NOT a bound.
    assert not [c for c in check_counts(
        ["Exactly 2 of the 10 questions are new, each with an answer paragraph"],
        payload) if c.unit == "paragraph" and c.bound == 2]


def test_bounds_the_engine_cannot_measure_are_named_not_hidden():
    """Only ~10% of compiled criteria parse into a deterministic check, and the
    rest are mostly bounds on units the grammar does not know. Those rest
    entirely on the reviewer — which is correct — but the run never said so."""
    from brain.criteria import unmeasurable_counts

    unmeasurable = unmeasurable_counts([
        "The FAQ contains exactly 8 questions",              # unit not in the grammar
        "At least one chart image is embedded in the report",
        "The report contains exactly 5 sections",            # measurable
        "Every claim is supported by retained evidence",     # states no bound
    ])
    assert "The FAQ contains exactly 8 questions" in unmeasurable
    assert any("chart image" in u for u in unmeasurable)
    assert not any("exactly 5 sections" in u for u in unmeasurable)
    assert not any("Every claim" in u for u in unmeasurable)
