"""Mediated team communication + model tiers (pure; no Docker/LLM)."""
from __future__ import annotations

import asyncio

from brain import config
from brain.factory import agent_system_prompt
from brain.memory.blackboard import Blackboard
from brain.models import AgentSpec
from brain.team import TeamChannel


def _channel(tmp_path, *, answers=None, max_questions=None):
    bb = Blackboard(tmp_path / "b.db", "team-test")

    async def ask_fn(stage, question):
        return (answers or {}).get(stage, f"answer about {stage}")

    ch = TeamChannel(agent_id="survey.w01", completed_stages=["gather"],
                     ask_fn=ask_fn, blackboard=bb, max_questions=max_questions)
    return ch, bb


def test_ask_upstream_answers_for_completed_stages(tmp_path):
    ch, bb = _channel(tmp_path, answers={"gather": "item 7 is https://x.example"})
    try:
        ok, text = asyncio.run(ch.ask("gather", "what is item 7's URL?"))
        assert ok and "https://x.example" in text
        # Unknown stage: refused with the list of what IS available.
        ok, text = asyncio.run(ch.ask("nonexistent", "?"))
        assert not ok and "gather" in text
    finally:
        bb.close()


def test_ask_upstream_budget(tmp_path):
    ch, bb = _channel(tmp_path, max_questions=1)
    try:
        ok, _ = asyncio.run(ch.ask("gather", "q1"))
        assert ok
        ok, text = asyncio.run(ch.ask("gather", "q2"))
        assert not ok and "budget" in text
    finally:
        bb.close()


def test_report_blocker_records_and_guides(tmp_path):
    ch, bb = _channel(tmp_path)
    try:
        text = ch.block("source is 404", needed="an alternative URL")
        assert "recorded" in text and "fabricate" not in text.split("recorded")[0]
        assert ch.blockers[0]["issue"] == "source is 404"
        assert bb.get("team.blockers")[0]["agent"] == "survey.w01"
    finally:
        bb.close()


def test_system_prompt_mentions_team_channel():
    spec = AgentSpec(id="w", role="Analyst", goal="analyze")
    with_team = agent_system_prompt(spec, [], team_stages=["gather", "survey"])
    assert "ask_upstream" in with_team and "gather, survey" in with_team
    assert "report_blocker" in with_team
    without = agent_system_prompt(spec, [])
    assert "ask_upstream" not in without


def test_model_tiers(monkeypatch):
    monkeypatch.setattr(config, "BRAIN_MODEL", None)
    monkeypatch.setattr(config, "BRAIN_MODEL_STRONG", None)
    monkeypatch.setattr(config, "BRAIN_MODEL_FAST", None)
    assert config.model_for("planner") is None          # everything falls to SDK default
    assert config.model_for("worker") is None

    monkeypatch.setattr(config, "BRAIN_MODEL", "claude-sonnet-5")
    assert config.model_for("worker") == "claude-sonnet-5"   # base fills both tiers

    monkeypatch.setattr(config, "BRAIN_MODEL_STRONG", "claude-opus-4-8")
    monkeypatch.setattr(config, "BRAIN_MODEL_FAST", "claude-haiku-4-5-20251001")
    assert config.model_for("planner") == "claude-opus-4-8"
    assert config.model_for("review") == "claude-opus-4-8"
    assert config.model_for("worker") == "claude-haiku-4-5-20251001"
