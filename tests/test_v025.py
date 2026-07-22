"""Epoch-aware acquisition persistence (v0.25.0).

Post-mortem: the authoring loop died at a static 3-attempt ceiling with the
fix IN HAND — the final attempt embodied a new solution category (enabled by
runtime evidence from the previous attempt) but was counted as "the third
repetition", and the run stopped to re-ask for a credential the user had
already provided. Attempts must be grouped by INFORMATION EPOCH: a new epoch
begins when genuinely new information enters the loop (a credential became
available, first runtime verification evidence, research delivered);
repetition counts only WITHIN an epoch; a fresh epoch gets grace attempts
before any judgment may stop the loop; and a continuation judgment — not a
bare counter — decides when the meaningful options are exhausted, bounded by
a higher static ceiling.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from brain import acquisition, config, workflow
from brain.acquisition import AcquisitionError, _continuation_digest
from brain.memory.blackboard import Blackboard
from brain.models import MethodOutcome
from brain.observability import NullTracer
from brain.orchestrator import Orchestrator
from brain.registry import Registry
from brain.supervisor import Supervisor


def _sub(emits=None, approach=None, rc=None):
    """A structurally valid submission for an observing (file-consuming) tool."""
    d = {"program": "p", "dockerfile": "FROM python:3.12-slim",
         "command": ["python", "-B", "/app/tool.py"],
         "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
         "env_map": {"q": "Q"}, "network": "none",
         "sample_input": {"q": "x"}, "accepts": ["files"]}
    if emits:
        d["emits"] = emits
    if approach:
        d["approach"] = approach
    if rc:
        d["requires_credential"] = rc
        d["side_effects"] = "external_read"   # credentialed => must declare
    return d


# --- the continuation digest (pure) --------------------------------------------- #
def test_continuation_digest_groups_by_epoch_and_names_credentials():
    records = [
        {"attempt": 1, "epoch": 0, "cls": "contract", "detail": "missing emits",
         "approach": ""},
        {"attempt": 2, "epoch": 0, "cls": "implementation", "detail": "token rejected",
         "approach": "strict-JSON credential parser"},
        {"attempt": 3, "epoch": 1, "cls": "contract", "detail": "missing emits",
         "approach": "multi-format credential parsing"},
    ]
    d = _continuation_digest("dl_tool", "download things", records,
                             ["initial approach", "credential TOK became available"],
                             ["TOK"], 4, 10)
    assert "epoch 0 — initial approach" in d
    assert "epoch 1 — credential TOK became available" in d
    assert d.index("attempt 3") > d.index("epoch 1 —")   # grouped under its epoch
    assert "CREDENTIALS THE RUN ALREADY HOLDS" in d and "NOT missing" in d
    assert "attempt 4" in d and "10" in d                # remaining budget visible
    assert "multi-format credential parsing" in d        # approach categories visible


# --- grace floor + judged early stop -------------------------------------------- #
def test_grace_then_judged_stop(tmp_path):
    """The first grace attempts run without judgment; past the floor, a
    stop-deciding supervisor ends the loop EARLY (well before the ceiling)
    with a classified error naming the judgment."""
    consults: list[tuple] = []

    async def decide(moment, digest):
        consults.append((moment, digest))
        return {"action": "stop", "reason": "same omission, nothing new"}

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    calls: list = []

    async def dev(need_key, description, previous=None, error=None):
        calls.append(error)
        return _sub()   # observing tool that never declares `emits` -> contract reject

    with pytest.raises(AcquisitionError) as ei:
        asyncio.run(acquisition._author_capability(
            "doomed", "a tool", manager=object(), registry=reg, broker=None,
            model=None, developer=dev, max_attempts=10, tracer=NullTracer(),
            supervisor=Supervisor(decide_fn=decide, tracer=NullTracer())))
    reg.close()
    assert len(calls) == config.ACQUISITION_EPOCH_GRACE   # only the grace attempts ran
    assert len(consults) == 1 and consults[0][0] == "continuation"
    assert "continuation judgment" in str(ei.value)
    assert ei.value.attempts == config.ACQUISITION_EPOCH_GRACE


# --- a credential arriving opens a new epoch and resets the grace ---------------- #
def test_credential_arrival_opens_new_epoch_and_resets_grace(tmp_path):
    """Attempts made WITHOUT a credential do not count against attempts made
    WITH it: when the credential becomes available the loop gets fresh grace
    attempts before the judgment may stop it — having just learned what was
    missing, you try several methods with it before concluding exhaustion."""
    consults: list[str] = []

    async def decide(moment, digest):
        consults.append(digest)
        if len(consults) == 1:
            return {"action": "continue", "reason": "credential not yet exercised",
                    "next_approach": "declare the credential and use it"}
        return {"action": "stop", "reason": "exhausted with the credential too"}

    class _Mgr:  # build always explodes -> packaging failure, no docker needed
        def build_image(self, tag, ctx):
            raise RuntimeError("boom")

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    broker = types.SimpleNamespace(secrets={"TOK": "value"})
    seen: list = []

    async def dev(need_key, description, previous=None, error=None):
        n = len(seen)
        seen.append(error)
        if n < 2:
            return _sub()   # epoch 0: no credential declared, no emits -> contract
        return _sub(emits="observation", rc="TOK", approach="use the provided TOK")

    with pytest.raises(AcquisitionError) as ei:
        asyncio.run(acquisition._author_capability(
            "cred_tool", "a tool", manager=_Mgr(), registry=reg, broker=broker,
            model=None, developer=dev, max_attempts=10, tracer=NullTracer(),
            supervisor=Supervisor(decide_fn=decide, tracer=NullTracer())))
    reg.close()
    h = "\n".join(ei.value.history)
    assert len(seen) == 4                 # 2 pre-credential + 2 fresh-grace attempts
    assert len(consults) == 2             # grace protected attempt 4 from judgment
    assert ei.value.attempts == 4
    assert "TOK became available" in h    # epoch marker rides in the history
    assert "[epoch 1]" in h
    assert "epoch 1" in consults[1] and "TOK" in consults[1]
    # The judge's next_approach was handed to the developer as guidance.
    assert "NEXT-APPROACH GUIDANCE" in (seen[2] or "")
    assert "declare the credential" in (seen[2] or "")


# --- first runtime evidence opens a new epoch ------------------------------------ #
def test_first_runtime_evidence_opens_epoch(tmp_path, monkeypatch):
    """The first verify failure is NEW INFORMATION — the tool now builds and
    runs, and its observed behavior is evidence no earlier attempt had — so
    later attempts form a new solution category."""
    monkeypatch.setattr(acquisition, "_generic_verify",
                        lambda cap, si, sf, mgr, env, ws: (False, "runtime said no", None))

    class _Mgr:
        def build_image(self, tag, ctx):
            pass

    reg = Registry(tmp_path / "r.db", tmp_path / "store")

    async def dev(need_key, description, previous=None, error=None):
        return _sub(emits="observation")

    with pytest.raises(AcquisitionError) as ei:
        asyncio.run(acquisition._author_capability(
            "rt_tool", "a tool", manager=_Mgr(), registry=reg, broker=None,
            model=None, developer=dev, max_attempts=2, tracer=NullTracer()))
    reg.close()
    h = "\n".join(ei.value.history)
    assert "first runtime verification evidence" in h
    assert "[epoch 0]" in h and "[epoch 1]" in h


# --- the static ceiling resolves from config ------------------------------------- #
def test_ceiling_resolves_from_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACQUISITION_MAX_ATTEMPTS", 4)
    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    n: list = []

    async def dev(need_key, description, previous=None, error=None):
        n.append(1)
        return _sub()

    with pytest.raises(AcquisitionError) as ei:
        asyncio.run(acquisition.acquire(
            "cfg_tool", manager=object(), registry=reg, broker=None,
            developer=dev, description="a tool", tracer=NullTracer()))
    reg.close()
    assert len(n) == 4 and ei.value.attempts == 4   # no supervisor: pure ceiling


# --- Supervisor.continuation: coercion, fail-open, budget ------------------------ #
def test_continuation_coerces_and_fails_open(monkeypatch):
    async def good(m, d):
        return {"action": "stop", "reason": "done"}
    assert asyncio.run(Supervisor(decide_fn=good).continuation("d")).action == "stop"

    async def cont(m, d):
        return {"action": "continue", "next_approach": "try X", "reason": "r"}
    dec = asyncio.run(Supervisor(decide_fn=cont).continuation("d"))
    assert dec.action == "continue" and dec.message == "try X"

    async def garbage(m, d):
        return {"action": "explode"}
    assert asyncio.run(Supervisor(decide_fn=garbage).continuation("d")).action == "continue"

    async def crash(m, d):
        raise RuntimeError("x")
    assert asyncio.run(Supervisor(decide_fn=crash).continuation("d")).action == "continue"

    monkeypatch.setattr(config, "SUPERVISOR_ENABLED", False)
    assert asyncio.run(Supervisor(decide_fn=good).continuation("d")).action == "continue"


def test_continuation_budget_fails_open():
    async def stopper(m, d):
        return {"action": "stop", "reason": "s"}
    sup = Supervisor(decide_fn=stopper, tracer=NullTracer())
    sup.cont_calls = config.CONTINUATION_MAX_CALLS
    assert asyncio.run(sup.continuation("d")).action == "continue"


# --- the stage-boundary digest names held credentials ---------------------------- #
def test_persistence_digest_names_held_credentials(tmp_path):
    """The stage-boundary judge must see which credentials the run already
    holds (names only) — 'authentication only the user can supply' was
    hallucinated for a credential sitting in the broker's cache."""
    bb = Blackboard(tmp_path / "b.db", "cred")
    broker = types.SimpleNamespace(secrets={"KAGGLE_JSON": "never-shown"})
    orch = Orchestrator(session="cred", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=broker, planner=None, resolver=None,
                        tracer=NullTracer())
    orch._run_ctx = ("obj", None, tmp_path / "ws")
    stages, _ = workflow.normalize_stages([
        {"id": "acq", "pattern": "single", "role": "D", "goal": "get the data"}])
    outcome = MethodOutcome(
        stage_id="acq", requirement="get the data", phase="acquisition",
        failure_class="contract", detail="could not author", attempts=3,
        attempt_history=[
            "attempt 1 [epoch 0]: contract — missing `emits` declaration",
            "— new information: credential KAGGLE_JSON became available (epoch 1 begins)",
            "attempt 3 [epoch 1]: contract — missing `emits` declaration"])
    orch._failures.append(outcome)
    digest = orch._persistence_digest(stages[0], outcome)
    bb.close()
    assert "CREDENTIALS THE RUN ALREADY HOLDS" in digest and "KAGGLE_JSON" in digest
    assert "never-shown" not in digest                       # names only, never values
    assert "epoch" in digest                                 # epoch instruction present
    assert "new information: credential KAGGLE_JSON" in digest  # markers survive
