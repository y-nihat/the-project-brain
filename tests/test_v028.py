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
            referent_resolved=False))
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
                             referent_resolved=True))
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
                      capture_output_files=False, **kw):
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


# --- cross-run recall: similarity, not surface area ------------------------------- #
def _history_db(tmp_path, runs):
    """runs: list of (run_id, objective, status, started_at)."""
    import sqlite3
    db = tmp_path / "h.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE runs (run_id TEXT, objective TEXT, status TEXT, "
                 "outcome TEXT, started_at TEXT)")
    conn.executemany("INSERT INTO runs VALUES (?,?,?,?,?)",
                     [(r[0], r[1], r[2], "", r[3]) for r in runs])
    conn.commit()
    conn.close()
    return db


def test_relevance_is_normalized_so_length_stops_winning(tmp_path):
    """Raw intersection size rewarded LENGTH: a long prior objective shares
    more tokens with anything by accident. The winner's workspace is seeded
    into the new run, so a bad pick does not merely fail to help — it puts the
    wrong files in front of the agents, which is how one run ended up unable
    to verify 'the 6 unaffected answers are unchanged' against an artifact
    that was never seeded."""
    from brain.memory.history import relevant_runs

    db = _history_db(tmp_path, [
        # Short and squarely on topic.
        ("run-focused", "parse the calendar export and list every event",
         "succeeded", "2026-07-01"),
        # Sprawling and only incidentally overlapping — wins on raw count.
        ("run-sprawling",
         "parse the quarterly finance workbook, list every formula, list every "
         "sheet, list every event handler, export the calendar of deadlines, "
         "and produce an extensive report covering every section in detail",
         "succeeded", "2026-07-02"),
    ])
    hits = relevant_runs(db, "parse the calendar export and list every event", limit=2)
    assert hits[0]["run_id"] == "run-focused", \
        "the most SIMILAR run must win, not the longest"


def test_recall_reaches_past_the_most_recent_fifty_runs(tmp_path):
    """The window was 50 while the corpus already held 107 runs, so the brain
    silently forgot more than half of what it had done."""
    from brain.memory.history import relevant_runs

    rows = [(f"run-noise-{i}", f"unrelated topic {i} zzz", "succeeded",
             f"2026-07-{(i % 28) + 1:02d}") for i in range(80)]
    rows.append(("run-old-match", "transcribe the standup recording",
                 "succeeded", "2026-01-01"))
    hits = relevant_runs(_history_db(tmp_path, rows),
                         "transcribe the standup recording", limit=3)
    assert any(h["run_id"] == "run-old-match" for h in hits)


def test_a_delivered_run_breaks_a_tie_but_never_outranks_similarity(tmp_path):
    """The old flat +1 on a raw count was magnitude-dependent — decisive for
    small overlaps, irrelevant for large ones. Status is a tiebreak."""
    from brain.memory.history import relevant_runs

    db = _history_db(tmp_path, [
        ("run-failed-same", "summarize the incident report", "failed", "2026-07-01"),
        ("run-ok-same", "summarize the incident report", "succeeded", "2026-07-02"),
    ])
    hits = relevant_runs(db, "summarize the incident report", limit=2)
    assert hits[0]["run_id"] == "run-ok-same", "equal similarity -> prefer delivered"


# --- run-folder outputs cross the redaction boundary ------------------------------ #
_CANARY = "sk-ant-" + "v028canary0123456789abcdef"


def test_answer_and_deliverable_are_scrubbed_on_disk(tmp_path, monkeypatch):
    """CHANGELOG.md tracked this as a known limitation: "Redaction does not yet
    cover every disk-writing path (sandbox stderr, failed-verify artifacts, the
    blackboard, run-folder outputs)".

    The asymmetry was exact — `tracer.RunHandle.finish` scrubbed the SAME
    string into `runs.outcome`, while `ANSWER.md` beside it got the raw text.
    The realistic path is an agent transcribing a credential it saw in a tool
    result or an attached file into its own prose; the database copy already
    assumes that can happen, which is why the file copy mattered."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    orch, bb = _orch(tmp_path, None, StubPlanner([]), "leak")
    try:
        out = tmp_path / "runs" / "leak" / "outputs"
        out.mkdir(parents=True)
        from brain.observability.redact import scrub_text
        (out / "ANSWER.md").write_text(scrub_text(f"the answer mentions {_CANARY} inline"))
        (out / "DELIVERABLE.md").write_text(scrub_text(f"payload with {_CANARY}"))
    finally:
        bb.close()

    for name in ("ANSWER.md", "DELIVERABLE.md"):
        blob = (tmp_path / "runs" / "leak" / "outputs" / name).read_text()
        assert _CANARY not in blob, f"{name} leaked a secret-shaped value"
        assert "redacted" in blob


def test_every_run_folder_text_write_passes_through_scrub():
    """Structural: the run folder is written from several places, and only one
    of them used to mask. Each text write of MODEL- or TOOL-derived content
    must go through `scrub_text`.

    Deliberately excluded: byte copies of user-supplied files. Scrubbing those
    would corrupt the operator's own data, and they never passed through an
    agent."""
    import re
    from brain import orchestrator as orch_mod
    from brain import acquisition as acq_mod

    for module, allowed_unscrubbed in (
            (orch_mod, {"locators.json"}),      # engine-derived, no model text
            (acq_mod, {"filename"})):           # authored build context, written verbatim
        text = open(module.__file__, encoding="utf-8").read()
        for line in text.splitlines():
            if ".write_text(" not in line or "scrub_text" in line:
                continue
            if any(a in line for a in allowed_unscrubbed):
                continue
            # Anything else writing text into a run folder must be scrubbed.
            assert "RUNS_DIR" not in line and "attempt_dir" not in line \
                and "outputs" not in line, f"unscrubbed run-folder write: {line.strip()}"


# --- the autonomy gate prices what it asks for ------------------------------------ #
def test_the_autonomy_gate_states_a_cost_not_just_a_count(tmp_path, monkeypatch):
    """The gate is the only place a human decides to spend UNATTENDED, and it
    asked to approve "up to N self-initiated run(s)" — a count. Recorded runs
    span $0.01 to $17.30, so a count carries almost no information about the
    commitment being made. Five iterations is somewhere between $0.05 and $86."""
    import sqlite3

    from brain import config
    from brain.session import BrainSession

    db = tmp_path / "b.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE runs (run_id TEXT, cost_usd REAL, started_at TEXT)")
    conn.executemany("INSERT INTO runs VALUES (?,?,?)",
                     [(f"r{i}", c, f"2026-07-{i + 1:02d}")
                      for i, c in enumerate([1.0, 2.0, 3.0, 12.0])])
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", db)

    estimate = BrainSession._pursuit_cost_estimate(object(), 5)
    assert "$" in estimate
    assert "15.00" in estimate, "median x budget"      # median of 4 samples = 3.0
    assert "60.00" in estimate, "worst observed x budget"
    assert "worst observed" in estimate


def test_the_cost_estimate_never_blocks_the_gate(tmp_path, monkeypatch):
    """Fail-open: no history, or an unreadable database, must leave the
    approval prompt working and unchanged."""
    from brain import config
    from brain.session import BrainSession

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "missing.db")
    assert BrainSession._pursuit_cost_estimate(object(), 3) == ""


# --- reuse: by contract when the key misses, and earned reuse made visible -------- #
def _cap(name, *, accepts=(), emits="observation", args=(), verified=True,
         quarantined=None):
    return Capability(
        name=name, description=f"{name} does a thing", image_tag="x", command=["r"],
        env_map={}, accepts=list(accepts), emits=emits, verified=verified,
        quarantined=quarantined,
        input_schema={"type": "object",
                      "properties": {a: {"type": "string"} for a in args}})


class _FakeRegistry:
    def __init__(self, caps):
        self._caps = {c.name: c for c in caps}

    def get(self, name):
        return self._caps.get(name)

    def all(self):
        return list(self._caps.values())

    def last_failures(self):
        return {}


def test_a_key_miss_reuses_a_capability_that_serves_the_same_contract():
    """`Resolver.resolve` was `registry.get(exact_name)` — its own docstring
    said "For M1 this is an exact lookup ... semantic matching is a later-phase
    upgrade", still true at v0.27. So the library was reachable only by the
    planner guessing the same snake_case string twice, and a synonym cost a
    full authoring session plus an image build to rebuild what already
    existed. The registry holds three Python executors, three PDF parsers and
    six writers."""
    from brain.models import CREATE, REUSE
    from brain.resolver import Resolver

    reg = _FakeRegistry([_cap("extract_pdf_text", accepts=[".pdf"],
                              emits="observation", args=["path"])])
    r = Resolver(reg)

    # Same contract under a different name -> reuse, not author.
    d = r.resolve("parse_pdf_to_text", contract={
        "accepts": [".pdf"], "emits": "observation",
        "example_input": {}, "arguments": ["path"]})
    assert d.kind == REUSE and d.capability.name == "extract_pdf_text"

    # A different output ROLE is a different contract -> author.
    assert r.resolve("render_pdf", contract={
        "accepts": [".pdf"], "emits": "artifact", "arguments": ["path"]}).kind == CREATE

    # An input kind the incumbent does not accept -> author.
    assert r.resolve("parse_docx", contract={
        "accepts": [".docx"], "emits": "observation", "arguments": ["path"]}).kind == CREATE


def test_contract_reuse_refuses_to_guess_when_several_match():
    """Ambiguity is not resolved by picking one: naming a winner would be a
    guess, and reuse must stay a deterministic fact."""
    from brain.models import CREATE
    from brain.resolver import Resolver

    reg = _FakeRegistry([_cap("writer_a", emits="artifact", args=["content", "filename"]),
                         _cap("writer_b", emits="artifact", args=["content", "filename"])])
    assert Resolver(reg).resolve("write_markdown_file", contract={
        "emits": "artifact", "arguments": ["content", "filename"]}).kind == CREATE


def test_quarantined_and_unverified_capabilities_are_never_contract_matched():
    from brain.models import CREATE
    from brain.resolver import Resolver

    reg = _FakeRegistry([
        _cap("bad", accepts=[".pdf"], args=["path"], quarantined="needed repair"),
        _cap("unverified_one", accepts=[".pdf"], args=["path"], verified=False)])
    assert Resolver(reg).resolve("read_pdf", contract={
        "accepts": [".pdf"], "emits": "observation", "arguments": ["path"]}).kind == CREATE


def test_the_catalog_states_what_has_actually_been_reused():
    """The planner chose from descriptions alone and could not tell a tool
    proven across 51 runs from one never picked since the day it was built."""
    from brain.registry import qualified_catalog

    reg = _FakeRegistry([_cap("proven", args=["q"]), _cap("dormant", args=["q"])])
    for c in reg.all():
        c.validated_engine = "epoch-1"
    cat = qualified_catalog(reg, "epoch-1", reuse_counts={"proven": 51})
    assert "reused in 51 prior run(s)" in cat["proven"]
    assert "never reused since it was built" in cat["dormant"]
    # Without counts the annotation is absent entirely (no false precision).
    assert "reused" not in qualified_catalog(reg, "epoch-1")["proven"]


# --- the eval harness can score history offline ----------------------------------- #
def test_the_engine_health_scenario_is_objective_neutral():
    """A scenario scorable against ANY recorded run cannot assert anything
    about the task. That is what makes a cross-version baseline possible
    without spending a cent."""
    from brain.evals.scenarios import ENGINE_HEALTH, OFFLINE_SCENARIOS, SCENARIOS, get

    task_shaped = {"answer_matches", "produced_file", "cites_url", "status_is",
                   "used_pattern", "min_workers", "answer_min_chars",
                   "answer_max_chars", "no_tools_used", "max_stages"}
    for name, _ in ENGINE_HEALTH.checks:
        assert name not in task_shaped, f"{name} asserts something task-specific"

    # Reachable by name, and kept OUT of the live suite so `run_suite` never
    # tries to execute a scenario that has no real objective.
    assert get("engine-health") is ENGINE_HEALTH
    assert ENGINE_HEALTH in OFFLINE_SCENARIOS
    assert ENGINE_HEALTH not in SCENARIOS


def test_scoring_recorded_runs_costs_nothing_and_rolls_up_by_version(tmp_path):
    """45 releases shipped with no cross-version behavioural baseline, and the
    harness built to provide one had not run since engine 0.1.0 — because
    running it LIVE costs money and minutes. `score_run` never needed either:
    the runs already happened and their events are on disk."""
    from brain.evals import runner
    from brain.observability.tracer import Tracer

    tracer = Tracer(db_path=tmp_path / "t.db", runs_dir=tmp_path / "runs")
    try:
        for ver, ratio_bad in (("0.27.0", False), ("0.28.0", True)):
            run = tracer.start_run("a task")
            tracer._conn.execute("UPDATE runs SET engine_version=? WHERE run_id=?",
                                 (ver, run.run_id))
            tracer.event("agent.finished", "s", status="ok", cost_usd=0.5)
            # A tool that mostly errors trips the health check.
            for _ in range(4 if ratio_bad else 0):
                tracer.event("tool.result", "cap", status="info", result="error")
            tracer.event("tool.result", "cap", status="info", result="ok")
            run.finish("succeeded", outcome="done")
        cards = runner.score_corpus("engine-health", db_path=tmp_path / "t.db",
                                    runs_dir=tmp_path / "runs")
        table = runner.render_corpus(cards, db_path=tmp_path / "t.db")
    finally:
        tracer.close()

    assert len(cards) == 2
    assert "0.27.0" in table and "0.28.0" in table
    assert "$0" in table
    assert "tripwire" in table          # states what it is, and is not
