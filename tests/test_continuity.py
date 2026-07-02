"""Data-layer persistence of agent outputs + cross-run memory (continuity).

All pure — no Docker, no LLM, no token.
"""
from __future__ import annotations

from brain.capabilities.runtime import _save_outputs
from brain.memory import history
from brain.models import AgentSpec
from brain.observability import Tracer
from brain.orchestrator import _persist_agent, _seed_prior_workspace


def test_persist_agent_writes_output_and_prompt_files(tmp_path):
    spec = AgentSpec(id="guide_writer", role="Guide Author", goal="write the guide",
                     capabilities=[], depends_on=["researcher"])
    _persist_agent(tmp_path, "run-x", spec, "SYSTEM PROMPT TEXT", "THE FULL OUTPUT")

    out = (tmp_path / "run-x" / "outputs" / "guide_writer.md").read_text()
    assert "THE FULL OUTPUT" in out and "Guide Author" in out
    prompt = (tmp_path / "run-x" / "agents" / "guide_writer.prompt.md").read_text()
    assert prompt == "SYSTEM PROMPT TEXT"


def test_save_outputs_handles_nested_paths_and_traversal(tmp_path):
    ws = tmp_path / "workspace"
    produced = _save_outputs(ws, "filegen", {
        "project/bot.py": b"print('hi')",
        "project/deploy/Dockerfile": b"FROM python",
        "top.txt": b"x",
        "../escape.txt": b"nope",           # must be refused
    })
    assert (ws / "filegen" / "project" / "bot.py").read_bytes() == b"print('hi')"
    assert (ws / "filegen" / "project" / "deploy" / "Dockerfile").exists()
    assert (ws / "filegen" / "top.txt").exists()
    assert not (ws / "escape.txt").exists()
    assert sorted(produced) == [
        "filegen/project/bot.py", "filegen/project/deploy/Dockerfile", "filegen/top.txt",
    ]


def _record_run(tracer, objective, status="succeeded", outcome=""):
    run = tracer.start_run(objective)
    run.finish(status, outcome=outcome)
    return run.run_id


def test_relevant_runs_ranks_matching_history(tmp_path):
    db = tmp_path / "b.db"
    tracer = Tracer(db, tmp_path / "runs")
    rid_bot = _record_run(tracer, "Build a chatbot that operates on Telegram",
                          outcome="bot project files and setup guide")
    _record_run(tracer, "Fetch the weather for three cities", outcome="csv report")
    tracer.close()

    got = history.relevant_runs(db, "Add a /help command to the Telegram chatbot you built")
    assert got and got[0]["run_id"] == rid_bot
    # Unrelated objective with zero overlap -> no history at all.
    assert history.relevant_runs(db, "quantum entanglement dynamics") == []


def test_history_block_and_seeding(tmp_path):
    db = tmp_path / "b.db"
    runs_dir = tmp_path / "runs"
    tracer = Tracer(db, runs_dir)
    rid = _record_run(tracer, "Build a chatbot that operates on Telegram")
    tracer.close()

    # Give the prior run an answer file + workspace files.
    (runs_dir / rid / "outputs").mkdir(parents=True)
    (runs_dir / rid / "outputs" / "ANSWER.md").write_text("Setup guide: talk to BotFather.")
    ws = runs_dir / rid / "workspace" / "filegen"
    ws.mkdir(parents=True)
    (ws / "bot.py").write_text("print('bot')")

    text, seed = history.history_block(db, runs_dir, "Extend the Telegram chatbot with a /help command")
    assert text and rid in text and "BotFather" in text and "bot.py" in text
    assert seed == rid

    # Seeding copies the prior files under prior/<run_id>/ in the NEW run.
    copied = _seed_prior_workspace(runs_dir, "run-new", rid)
    assert copied == [f"prior/{rid}/filegen/bot.py"]
    assert (runs_dir / "run-new" / "workspace" / "prior" / rid / "filegen" / "bot.py").exists()


def test_history_block_empty_when_no_db(tmp_path):
    text, seed = history.history_block(tmp_path / "missing.db", tmp_path / "runs", "anything")
    assert text is None and seed is None
