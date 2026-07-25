"""The conversation: the unit of record above a run.

Offline (no Docker/SDK/token). Covers the four things that make a conversation
real: the schema migrates onto a store that predates it, turns are numbered from
the record, a live conversation is protected from another process's cleanup, and
a recorded conversation can be read back and continued.
"""
from __future__ import annotations

import sqlite3

import pytest

from brain import conversation as conv
from brain.ledger import Ledger
from brain.observability import Tracer
from brain.observability import view


def _legacy_db(path) -> None:
    """A runs table exactly as it was before conversations existed."""
    c = sqlite3.connect(str(path))
    c.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, objective TEXT, status TEXT, model TEXT,
            engine_version TEXT, started_at TEXT, finished_at TEXT, n_events INTEGER,
            n_containers INTEGER, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,
            n_approvals INTEGER, duration_ms INTEGER, outcome TEXT
        );
        INSERT INTO runs (run_id, objective, status, started_at, cost_usd)
        VALUES ('run-old-1', 'something from before', 'succeeded', '2026-01-01T00:00:00', 1.5);
        """
    )
    c.commit()
    c.close()


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #
def test_migrates_a_store_that_predates_conversations(tmp_path):
    """CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so without a
    migration the new columns would exist only for fresh installations — and the
    first INSERT against a real store would fail."""
    db = tmp_path / "t.db"
    _legacy_db(db)

    tracer = Tracer(db, tmp_path / "runs", conversation_id="conv-x")
    run = tracer.start_run("a new thing")
    run.finish("succeeded", outcome="done")
    tracer.close()

    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
        assert {"conversation_id", "turn", "origin"} <= cols
        old = c.execute("SELECT * FROM runs WHERE run_id='run-old-1'").fetchone()
        assert old["conversation_id"] is None and old["turn"] is None
        new = c.execute("SELECT * FROM runs WHERE run_id=?", (run.run_id,)).fetchone()
        assert new["conversation_id"] == "conv-x" and new["turn"] == 1
    finally:
        c.close()


def test_pre_conversation_runs_render_as_archive(tmp_path):
    """Runs recorded before conversations existed stay ungrouped and are named
    for what they are. Grouping them after the fact would fabricate structure."""
    db = tmp_path / "t.db"
    _legacy_db(db)
    Tracer(db, tmp_path / "runs").close()          # migrate only

    listing = view.render_list(db)
    assert "run-old-1" in listing and view.ARCHIVE_LABEL in listing
    assert view.list_conversations(db) == []
    assert "no conversations recorded" in view.render_conversations(db)


def test_view_tolerates_a_store_without_the_columns(tmp_path):
    """A trace store that was never opened by a migrating Tracer must not make
    the viewer explode — it has no conversations, that is all."""
    db = tmp_path / "t.db"
    _legacy_db(db)
    assert view.list_conversations(db) == []
    assert "not found" in view.render_conversation(db, "conv-nope")
    assert view.ARCHIVE_LABEL in view.render_list(db)


# --------------------------------------------------------------------------- #
# Turns
# --------------------------------------------------------------------------- #
def test_turns_are_numbered_from_the_record(tmp_path):
    db, runs = tmp_path / "t.db", tmp_path / "runs"
    tracer = Tracer(db, runs, conversation_id="conv-a")
    first = tracer.start_run("one")
    first.finish("succeeded", outcome="1")
    second = tracer.start_run("two")
    second.finish("succeeded", outcome="2")
    tracer.close()

    rows = conv.turns(db, "conv-a")
    assert [r["turn"] for r in rows] == [1, 2]
    assert conv.last_user_turn(db, "conv-a")["objective"] == "two"

    # A LATER process continues the numbering without being told where it left
    # off — the ordinal is derived from the record, not from process state.
    resumed = Tracer(db, runs, conversation_id="conv-a")
    third = resumed.start_run("three")
    third.finish("succeeded", outcome="3")
    resumed.close()
    assert [r["turn"] for r in conv.turns(db, "conv-a")] == [1, 2, 3]


def test_self_initiated_runs_do_not_consume_a_turn(tmp_path):
    """A pursuit run is a run of the brain, not a thing the user said. It is
    recorded under the turn that authorized it."""
    db = tmp_path / "t.db"
    tracer = Tracer(db, tmp_path / "runs", conversation_id="conv-b")
    tracer.start_run("the user's turn").finish("succeeded", outcome="ok")
    tracer.start_run("brain's own iteration", origin="pursuit").finish("succeeded")
    tracer.start_run("the user again").finish("succeeded", outcome="ok")
    tracer.close()

    rows = conv.turns(db, "conv-b")
    assert [(r["turn"], r["origin"]) for r in rows] == [
        (1, "user"), (1, "pursuit"), (2, "user")]
    # The referent for a follow-up is the last thing the USER said.
    assert conv.last_user_turn(db, "conv-b")["objective"] == "the user again"


def test_rendering_a_conversation_shows_its_turns_in_order(tmp_path):
    db = tmp_path / "t.db"
    tracer = Tracer(db, tmp_path / "runs", conversation_id="conv-c")
    tracer.start_run("first question").finish("succeeded", outcome="first answer")
    tracer.start_run("second question").finish("succeeded", outcome="second answer")
    tracer.close()

    text = view.render_conversation(db, "conv-c")
    assert text.index("first question") < text.index("second question")
    assert "Turn 1" in text and "Turn 2" in text
    assert "first answer" in text and "second answer" in text
    assert "conv-c" in view.render_conversations(db)


def test_resolve_accepts_prefixes_and_refuses_ambiguity(tmp_path):
    db = tmp_path / "t.db"
    for cid in ("conv-aaa-1", "conv-aaa-2", "conv-bbb-1"):
        t = Tracer(db, tmp_path / "runs", conversation_id=cid)
        t.start_run(f"in {cid}").finish("succeeded")
        t.close()

    assert conv.resolve(db, "conv-bbb-1") == "conv-bbb-1"
    assert conv.resolve(db, "conv-bbb") == "conv-bbb-1"       # unique prefix
    assert conv.resolve(db, "conv-aaa") is None               # ambiguous: refuse
    assert conv.resolve(db, "nope") is None
    assert conv.resolve(db, "last") == "conv-bbb-1"           # most recent


# --------------------------------------------------------------------------- #
# Liveness: a live conversation is not a corpse
# --------------------------------------------------------------------------- #
def test_a_held_conversation_is_reported_live_and_a_released_one_is_not(tmp_path):
    locks = tmp_path / "locks"
    held = conv.Liveness(locks, "conv-live")
    assert held.held
    assert conv.live_ids(locks) == {"conv-live"}

    held.release()
    assert conv.live_ids(locks) == set()
    # A leftover lock file whose lock can be taken is a leftover, not a session.
    (locks).mkdir(parents=True, exist_ok=True)
    (locks / "conv-dead.lock").write_text("999999\n")
    assert conv.live_ids(locks) == set()
    assert not (locks / "conv-dead.lock").exists()


def test_a_live_conversations_running_rows_are_not_marked_aborted(tmp_path):
    """Before conversations, a second process marked the first's ACTIVE run
    aborted, because 'running' was assumed to mean 'a corpse'."""
    db = tmp_path / "t.db"
    mine = Tracer(db, tmp_path / "runs", conversation_id="conv-mine")
    mine.start_run("still working on this")          # deliberately unfinished
    theirs = Tracer(db, tmp_path / "runs", conversation_id="conv-orphan")
    theirs.start_run("nobody is running this")       # deliberately unfinished

    janitor = Tracer(db, tmp_path / "runs", conversation_id="conv-janitor")
    assert janitor.abort_stale_runs(skip_conversations={"conv-mine"}) == 1

    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    try:
        by_conv = {r["conversation_id"]: r["status"] for r in
                   c.execute("SELECT conversation_id, status FROM runs")}
    finally:
        c.close()
    assert by_conv["conv-mine"] == "running"         # untouched: still live
    assert by_conv["conv-orphan"] == "aborted"       # genuinely orphaned
    for t in (mine, theirs, janitor):
        t.close()


def test_a_live_conversations_leak_evidence_survives_another_startup(tmp_path):
    """A running container row IS the leak evidence its own run checks at the
    end. Rewriting another conversation's rows turned a leaking run clean."""
    ledger = Ledger(tmp_path / "t.db")

    class _Pol:
        mem, cpus, pids, network, ttl_seconds = "256m", 1.0, 64, "none", 60

    ledger.record_start(id="c-live", session="run-1", conversation="conv-mine",
                        capability=None, image="i", command=["x"], policy=_Pol())
    ledger.record_start(id="c-orphan", session="run-2", conversation="conv-gone",
                        capability=None, image="i", command=["x"], policy=_Pol())

    assert ledger.mark_stale_reaped(keep_conversations={"conv-mine"}) == 1
    still_running = {r["id"] for r in ledger.find_leaks()}
    assert still_running == {"c-live"}
    ledger.close()


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def test_session_id_is_exported_only_when_there_is_a_conversation():
    from brain.observability import otel_export

    grouped = otel_export._attributes("agent.session", "planner", "run-1", {}, "conv-z")
    assert grouped["session.id"] == "conv-z"
    # An archive run must NOT collapse into a catch-all session.
    ungrouped = otel_export._attributes("agent.session", "planner", "run-1", {}, None)
    assert "session.id" not in ungrouped


# --------------------------------------------------------------------------- #
# Continuing a recorded conversation
# --------------------------------------------------------------------------- #
def test_a_recorded_conversation_can_be_read_back_and_continued(tmp_path, monkeypatch):
    """What comes back is the record's referents — what was asked, what came
    back, which files the turn produced. Never live state."""
    from brain import config, session as session_mod

    db, runs = tmp_path / "t.db", tmp_path / "runs"
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "RUNS_DIR", runs)

    tracer = Tracer(db, runs, conversation_id="conv-r")
    first = tracer.start_run("build the thing")
    (runs / first.run_id / "outputs").mkdir(parents=True, exist_ok=True)
    (runs / first.run_id / "outputs" / "DELIVERABLE.md").write_text("# the thing\nbody")
    (runs / first.run_id / "workspace").mkdir(parents=True, exist_ok=True)
    (runs / first.run_id / "workspace" / "index.html").write_text("<html>")
    first.finish("succeeded", outcome="built it")
    tracer.close()

    referents = session_mod._turn_referents(first.run_id, "build the thing", "succeeded")
    assert "the thing" in referents["deliverable"]
    assert referents["files"] == ["index.html"]

    last = conv.last_user_turn(db, "conv-r")
    assert last["run_id"] == first.run_id and last["turn"] == 1


def _isolated_session(tmp_path, monkeypatch, **kwargs):
    """A BrainSession rooted entirely inside tmp_path (no Docker, no LLM call)."""
    from brain import config
    from brain.session import BrainSession

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for name, sub in (("DATA_DIR", ""), ("DB_PATH", "brain.db"), ("RUNS_DIR", "runs"),
                      ("LOCKS_DIR", "locks"), ("CAPABILITIES_STORE", "caps")):
        monkeypatch.setattr(config, name, tmp_path / sub if sub else tmp_path)
    return BrainSession(auto_export=False, output_fn=lambda _s: None, **kwargs)


def test_a_session_resumes_the_last_turn_of_a_recorded_conversation(tmp_path, monkeypatch):
    from brain import config

    session = _isolated_session(tmp_path, monkeypatch)
    cid = session.conversation_id
    run = session.tracer.start_run("build the thing")
    outputs = config.RUNS_DIR / run.run_id / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "DELIVERABLE.md").write_text("# the thing")
    run.finish("succeeded", outcome="built it")
    session.close()                     # the process ends; the record remains

    later = _isolated_session(tmp_path, monkeypatch, resume=cid)
    try:
        assert later.conversation_id == cid
        assert later._last_run is not None
        assert later._last_run["run_id"] == run.run_id
        assert "the thing" in later._last_run["deliverable"]
        # Its next turn continues the numbering rather than restarting it.
        nxt = later.tracer.start_run("now change it")
        assert nxt.turn == 2
        nxt.finish("succeeded", outcome="changed")
    finally:
        later.close()


def test_resuming_a_conversation_that_was_never_recorded_starts_clean(tmp_path, monkeypatch):
    session = _isolated_session(tmp_path, monkeypatch, resume="conv-never-existed")
    try:
        assert session.conversation_id == "conv-never-existed"
        assert session._last_run is None      # nothing to continue from
    finally:
        session.close()


def test_resuming_a_conversation_held_by_a_live_process_is_refused(tmp_path):
    """Two processes appending turns to one conversation would interleave into
    a record neither of them said."""
    locks = tmp_path / "locks"
    holder = conv.Liveness(locks, "conv-busy")
    assert holder.held

    second = conv.Liveness(locks, "conv-busy")
    if second.held:          # platform without advisory locks: nothing to assert
        pytest.skip("advisory locks unavailable on this platform")
    assert conv.live_ids(locks, own=None) == {"conv-busy"}
    holder.release()


# --------------------------------------------------------------------------- #
# Continuity follows a resolved referent, not a position
# --------------------------------------------------------------------------- #
def _drive_turn(session, monkeypatch, objective, *, referent):
    """Run one turn to the point of orchestration and capture what the
    orchestrator was handed. Returns the captured kwargs."""
    import asyncio

    from brain import session as session_mod
    from brain.analyze import Analyst

    captured: dict = {}

    class _Orch:
        classification = "succeeded"
        aspiration: dict = {}

        def __init__(self, **kw):
            captured["ctor"] = kw

        async def run(self, objective, **kw):
            captured.update(kw)
            captured["objective"] = objective
            return "the work"

    monkeypatch.setattr(session_mod, "Orchestrator", _Orch)

    async def judge(_prompt):
        return {"resolution": "escalate", "referent_run_id": referent or "",
                "reason": "r"}

    session._analyst = lambda: Analyst(judge_fn=judge)
    asyncio.run(session.run_objective(objective))
    return captured


def test_a_turn_that_refers_to_an_earlier_one_is_seeded_from_exactly_that_run(
        tmp_path, monkeypatch):
    """The v0.30 session-side continuity contract, driven rather than grepped.

    Its only previous coverage was four substring checks over session.py, one
    of which (`'_last_run' in src`) was vacuous. Relevance is lexical, so with
    an unrelated follow-up the ONLY thing that can produce a seed here is the
    resolved referent.
    """
    session = _isolated_session(tmp_path, monkeypatch)
    try:
        first = session.tracer.start_run("design a landing page")
        first.finish("succeeded", outcome="made page.html")

        got = _drive_turn(session, monkeypatch, "now stand it up",
                          referent=first.run_id)
        assert got["seed_run"] == first.run_id
        assert got["referent_resolved"] is True
        # The planner is told what it refers to, and told the earlier turns are
        # context — not that this turn continues them.
        ctx = got["session_context"] or ""
        assert "REFERENT" in ctx and first.run_id in ctx
        assert "does NOT necessarily continue" in ctx
    finally:
        session.close()


def test_a_turn_that_refers_to_nothing_inherits_nothing(tmp_path, monkeypatch):
    """Changing the subject used to inherit the previous deliverable as
    target-tier evidence about a question it never observed."""
    session = _isolated_session(tmp_path, monkeypatch)
    try:
        first = session.tracer.start_run("design a landing page")
        first.finish("succeeded", outcome="made page.html")

        got = _drive_turn(session, monkeypatch,
                          "what is the capital of Chile", referent=None)
        assert got["seed_run"] is None          # relevance finds nothing either
        assert got["referent_resolved"] is False
        assert "REFERENT" not in (got["session_context"] or "")
    finally:
        session.close()


def test_independent_trial_mode_kills_both_carry_over_paths(tmp_path, monkeypatch):
    """An operator re-running the SAME task as separate comparable attempts.

    The flag cleared the relevance seed and the next statement unconditionally
    re-set it from the previous run, so every repeat after the first was seeded
    from the attempt it was supposed to be independent of. Both paths — the
    relevance seed AND a resolved referent — must be suppressed.
    """
    monkeypatch.setenv("BRAIN_DISABLE_HISTORY_SEED", "1")
    session = _isolated_session(tmp_path, monkeypatch)
    try:
        first = session.tracer.start_run("design a landing page")
        first.finish("succeeded", outcome="made page.html")

        got = _drive_turn(session, monkeypatch, "now stand it up",
                          referent=first.run_id)
        assert got["seed_run"] is None
        assert got["referent_resolved"] is False
        assert got["history"] == ""
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Parts of the API nothing reached
# --------------------------------------------------------------------------- #
def test_recent_ids_orders_by_last_activity(tmp_path):
    """Untested entirely, and it is what `resolve('last')` and the REPL's
    conversation listing both stand on."""
    db, runs = tmp_path / "t.db", tmp_path / "runs"
    for cid, obj in (("conv-old", "first"), ("conv-new", "second")):
        t = Tracer(db, runs, conversation_id=cid)
        t.start_run(obj).finish("succeeded", outcome="x")
        t.close()
    assert conv.recent_ids(db) == ["conv-new", "conv-old"]
    assert conv.recent_ids(db, limit=1) == ["conv-new"]
    # A store with no conversations answers honestly rather than raising.
    assert conv.recent_ids(tmp_path / "absent.db") == []


def test_a_crashed_process_leaves_a_lock_that_recovers(tmp_path):
    """The failure mode a file lock INTRODUCES and a database row did not: a
    process dies without releasing, and its conversation must not be marked
    live forever. Nothing covered stale-lock recovery."""
    locks = tmp_path / "locks"
    locks.mkdir(parents=True)
    # Exactly what a killed process leaves behind: the file, no holder.
    (locks / "conv-crashed.lock").write_text("4242\n")

    assert conv.live_ids(locks) == set()            # not live: nobody holds it
    assert not (locks / "conv-crashed.lock").exists()   # and it is cleaned up
    # The conversation can then be taken over.
    taken = conv.Liveness(locks, "conv-crashed")
    assert taken.held
    taken.release()


def test_resolving_a_conversation_says_nothing_about_who_holds_it(tmp_path):
    """`resolve` answers "does this id exist in the record", `live_ids`
    answers "is anyone using it". Conflating them would make a busy
    conversation unresolvable and an idle one unjoinable."""
    db, runs, locks = tmp_path / "t.db", tmp_path / "runs", tmp_path / "locks"
    t = Tracer(db, runs, conversation_id="conv-busy")
    t.start_run("something").finish("succeeded", outcome="x")
    t.close()

    holder = conv.Liveness(locks, "conv-busy")
    try:
        assert conv.resolve(db, "conv-busy") == "conv-busy"      # still resolvable
        assert "conv-busy" in conv.live_ids(locks)               # and held
    finally:
        holder.release()
