"""v0.15 — the round-survival + evidence-provisioning release (from a 5-task
external campaign): parallel tool results — each inside the per-result budget —
summed past the SDK's 1 MiB frame and killed a 1-round review; a planner burned
three turns on a '$PARAMETER_NAME' argument envelope answered 'plan recorded',
then died of turn budget with its valid plan discarded; an acquisition failed
identically three times under three fresh contract names while the supervisor
read the repeats as distinct bugs; the stall watchdog killed a developer that
was waiting on its own requested research; a revision run could not verify
preserve-discipline because the prior deliverable existed only as narration;
and a sound absence claim was voided because the corpus enumeration existed
nowhere as a citable file."""
from __future__ import annotations

import asyncio
import json

import pytest

from brain import acquisition, config, evidence
from brain.memory.blackboard import Blackboard
from brain.models import StageSpec
from brain.observability import NullTracer
from brain.observability.tracer import AgentStallError, _iter_with_watchdog
from brain.orchestrator import Orchestrator, _failure_signature
from brain.planner import StubPlanner, _make_emit_plan, _unwrap_plan_args


class RecordingTracer(NullTracer):
    def __init__(self):
        self.events: list[tuple] = []

    def event(self, kind, subject, status="info", **data):
        self.events.append((kind, subject, status, data))

    def kinds(self):
        return [k for k, *_ in self.events]


def _orch(tmp_path, runner, planner, session, tracer=None):
    bb = Blackboard(tmp_path / f"{session}.db", session)
    orch = Orchestrator(session=session, manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner, tracer=tracer)
    return orch, bb


# --- transport: per-result budgets must share the frame --------------------------- #
def test_result_budget_is_a_frame_share():
    """A turn's PARALLEL tool results share one SDK frame, so a per-result
    budget equal to the whole frame budget is the exact defect this fixed.

    The old assertion was `per * SHARE <= SAFE_BYTES`, which is the
    floor-division identity (A//B)*B <= A and cannot fail for any values.
    """
    per = config.transport_result_budget()
    # The share must actually divide: SHARE=1 gives every result the whole
    # frame, which is the bug. It is env-driven, so this is a real guard.
    assert config.TRANSPORT_FRAME_SHARE >= 2
    assert per < config.TRANSPORT_SAFE_RESULT_BYTES
    assert per * config.TRANSPORT_FRAME_SHARE < 1024 * 1024


def test_a_tool_result_is_actually_fitted_to_the_frame_share():
    """The CALL SITE, which nothing reached: the budget existing is not the
    guarantee — a result being cut to it is. The failure this constant exists
    to prevent (parallel results jointly overflowing one SDK frame and killing
    the session) had no regression test at all."""
    from brain.capabilities.runtime import fit_result_text

    budget = config.transport_result_budget()
    fitted = fit_result_text("x" * (budget * 3), budget - 16384)
    assert len(fitted.encode()) <= budget - 16384
    # A result already inside the budget is returned unharmed.
    small = "a short tool result"
    assert fit_result_text(small, budget) == small


# --- resilience: an infra-crashed review round is retried without cost ------------ #
def test_crashed_round_is_refunded_on_a_one_round_review(tmp_path, monkeypatch):
    """The campaign shape: rounds=1, the only round dies of a transport fault.
    The retry must run the review instead of shipping the draft unverified."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()

    async def runner(_t, name, prompt, options):
        if name == "check.r1":
            raise RuntimeError("JSON message exceeded maximum buffer size of 1048576")
        if name == "check.r2":
            assert "CRASHED mid-session" in prompt
            return "VERDICT: approved"
        return "```deliverable\nthe work\n```"

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write"),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], rounds=1)])
    orch, bb = _orch(tmp_path, runner, planner, "refund1", tracer)
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    failed = [d for k, _s, _st, d in tracer.events if k == "review.round_failed"]
    assert failed and failed[0].get("round_refunded") is True
    assert orch._review_approved
    assert orch.classification == "succeeded"


def test_crash_refunds_are_bounded(tmp_path, monkeypatch):
    """Only REVIEW_CRASH_RETRIES crashes are free: with rounds=1 and two
    crashes, the review exhausts — no infinite crash loop."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    rounds_run: list[str] = []

    async def runner(_t, name, prompt, options):
        if name.startswith("check.r"):
            rounds_run.append(name)
            raise RuntimeError("frame limit again")
        return "```deliverable\nthe work\n```"

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write"),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], rounds=1)])
    orch, bb = _orch(tmp_path, runner, planner, "refund2", tracer)
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    assert rounds_run == ["check.r1", "check.r2"]   # 1 round + 1 refund, then stop
    assert "review.exhausted" in tracer.kinds()
    assert not orch._review_approved


# --- resilience: plan-emission envelopes and salvage ------------------------------- #
def test_unwrap_recovers_placeholder_envelope():
    payload = {"stages": [{"id": "s", "pattern": "single", "role": "r", "goal": "g"}],
               "success_criteria": ["c"]}
    fixed, note = _unwrap_plan_args({"$PARAMETER_NAME": payload})
    assert fixed == payload and "envelope" in note
    # A JSON-string envelope is recovered too.
    fixed, _ = _unwrap_plan_args({"input": json.dumps(payload)})
    assert fixed["success_criteria"] == ["c"]
    # Recognized top-level keys pass through untouched, extras kept.
    args = dict(payload, future_key="kept")
    assert _unwrap_plan_args(args) == (args, "")


def test_malformed_emission_gets_corrective_error_not_success():
    """The campaign failure: three malformed calls were each answered 'plan
    recorded' — positive feedback for capturing nothing."""
    captured: dict = {}
    tracer = RecordingTracer()
    handler = _make_emit_plan(captured, tracer, "planner")
    out = asyncio.run(handler({"bogus": "x"}))
    assert out.get("is_error") and "stages" in out["content"][0]["text"]
    assert captured == {}
    assert "plan.args_rejected" in tracer.kinds()
    # The repaired-envelope path records and reports success.
    out = asyncio.run(handler({"$PARAMETER_NAME": {"stages": [
        {"id": "s", "pattern": "single", "role": "r", "goal": "g"}]}}))
    assert not out.get("is_error")
    assert captured["stages"] and "plan.args_repaired" in tracer.kinds()


def _stub_sdk(monkeypatch, emitted: list):
    """Route the planner's in-method SDK imports to inert stand-ins: `tool`
    hands the raw handler to the test, `create_sdk_mcp_server` is a no-op."""
    import claude_agent_sdk

    def fake_tool(_name, _desc, _schema):
        def deco(fn):
            emitted.append(fn)
            return fn
        return deco

    monkeypatch.setattr(claude_agent_sdk, "tool", fake_tool)
    monkeypatch.setattr(claude_agent_sdk, "create_sdk_mcp_server",
                        lambda **_kw: {"name": _kw.get("name", "stub")})


def test_valid_plan_survives_planner_session_death(monkeypatch):
    """The planner emits a valid plan, then the session dies of turn budget:
    the captured plan must be used, not discarded with the exception."""
    from brain import planner as planner_mod

    emitted: list = []
    _stub_sdk(monkeypatch, emitted)

    async def fake_trace_query(_tracer, _name, _prompt, _options, **_kw):
        handler = emitted[-1]
        await handler({"stages": [{"id": "s1", "pattern": "single",
                                   "role": "writer", "goal": "write it"}],
                       "success_criteria": ["done"]})
        raise RuntimeError("Claude Code returned an error result: "
                           "Reached maximum number of turns (6)")

    import brain.observability as obs
    monkeypatch.setattr(obs, "trace_query", fake_trace_query)

    tracer = RecordingTracer()
    plan = asyncio.run(planner_mod.Planner(tracer=tracer).plan("objective"))
    assert [s.id for s in plan.stages] == ["s1"]
    assert plan.success_criteria == ["done"]
    assert "plan.salvaged" in tracer.kinds()


def test_planner_death_without_valid_plan_still_raises(monkeypatch):
    from brain import planner as planner_mod

    _stub_sdk(monkeypatch, [])

    async def fake_trace_query(*_a, **_kw):
        raise RuntimeError("Reached maximum number of turns (6)")

    import brain.observability as obs
    monkeypatch.setattr(obs, "trace_query", fake_trace_query)
    with pytest.raises(RuntimeError):
        asyncio.run(planner_mod.Planner(tracer=RecordingTracer()).plan("objective"))


# --- adaptation: run-scoped, cross-contract failure memory ------------------------ #
def test_failure_log_crosses_contract_renames(tmp_path):
    """An approach that failed under one contract name must be visible to the
    developer authoring under a DIFFERENT name — the campaign run re-attempted
    one approach three times under three fresh names, blind each time."""
    from types import SimpleNamespace

    from brain.approval import ApprovalBroker
    from brain.registry import Registry

    reg = Registry(tmp_path / "r.db", tmp_path / "store")

    class BoomManager:
        def build_image(self, tag, build_context):
            raise RuntimeError("model download during build: connection refused")

    seen_tasks: list[str] = []

    async def stub_developer(need_key, description, previous=None, error=None):
        seen_tasks.append(description)
        return {"program": "print(1)", "dockerfile": "FROM scratch",
                "command": ["python"], "input_schema": {"type": "object"},
                "env_map": {}, "network": "none", "sample_input": {},
                "requires_credential": None}

    log: list = []
    broker = ApprovalBroker(auto="deny")
    with pytest.raises(acquisition.AcquisitionError):
        asyncio.run(acquisition.acquire(
            "observe_signal_a", manager=BoomManager(), registry=reg, broker=broker,
            developer=stub_developer, description="observe the signal",
            tracer=NullTracer(), max_attempts=1, failure_log=log))
    assert log and "observe_signal_a" in log[0] and "connection refused" in log[0]

    with pytest.raises(acquisition.AcquisitionError):
        asyncio.run(acquisition.acquire(
            "observe_signal_b", manager=BoomManager(), registry=reg, broker=broker,
            developer=stub_developer, description="observe the signal",
            tracer=NullTracer(), max_attempts=1, failure_log=log))
    # The SECOND contract's developer saw the FIRST contract's failure.
    assert "APPROACHES THAT ALREADY FAILED THIS RUN" in seen_tasks[1]
    assert "observe_signal_a" in seen_tasks[1]
    assert "APPROACHES THAT ALREADY FAILED" not in seen_tasks[0]
    reg.close()


def test_persistence_digest_counts_repeated_signatures(tmp_path):
    """Deterministic repeat detection: identical failure shapes under
    different names are counted and named a plateau for the supervisor."""
    from brain.models import MethodOutcome

    bb = Blackboard(tmp_path / "b.db", "digest")
    orch = Orchestrator(session="digest", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=StubPlanner([]),
                        resolver=None, session_runner=None, tracer=NullTracer())
    try:
        orch._run_ctx = ("objective", None, tmp_path / "ws")
        orch._failures = []
        orch._outputs = {}
        orch._recoveries = 0
        orch._sessions = 0
        orch._acq_failures = [
            "'observe_a' build [packaging]: error 4711: model download failed",
            "'observe_b' build [packaging]: error 4712: model download failed",
        ]
        digest = orch._persistence_digest(
            StageSpec(id="s", pattern="single", role="r", goal="observe"),
            MethodOutcome(stage_id="s", requirement="observe", phase="acquisition",
                          failure_class="packaging", detail="d"))
    finally:
        bb.close()
    assert "REPEATED ERROR SIGNATURES" in digest
    assert "seen 2x" in digest and "PLATEAU" in digest


def test_failure_signature_collapses_noise_but_keeps_distinct_failures_apart():
    """Both directions. Only the collapsing half was asserted, which a constant
    -returning implementation satisfies — while making the supervisor declare a
    false PLATEAU on two genuinely different failures and abandon a viable
    approach."""
    a = _failure_signature("Error 1234: build cand-9f21ab3c failed at step 7")
    b = _failure_signature("Error 987: build cand-11deadbeef failed at step 9")
    assert a == b                       # volatile ids and digits are noise
    c = _failure_signature("error 1: model download failed")
    d = _failure_signature("error 1: no such file or directory")
    assert c != d                       # different failures stay different


# --- resilience: waiting on delegated work is not a stall -------------------------- #
def test_watchdog_spares_sessions_waiting_on_delegated_work():
    from brain.observability import activity

    async def slow_then_yield():
        await asyncio.sleep(0.35)
        yield "m1"

    async def run_guarded():
        with activity.working():
            return [m async for m in _iter_with_watchdog(
                slow_then_yield(), 0.1, 5, "agent 'x'")]

    assert asyncio.run(run_guarded()) == ["m1"]
def test_watchdog_total_budget_survives_delegated_work():
    from brain.observability import activity

    async def silent():
        await asyncio.sleep(10)
        yield "never"

    async def run_guarded():
        with activity.working():
            async for _ in _iter_with_watchdog(silent(), 0.05, 0.3, "agent 'x'"):
                pass

    with pytest.raises(AgentStallError) as e:
        asyncio.run(run_guarded())
    assert "total session budget" in str(e.value)


# --- grounding: prior deliverable is evidence in a revision run -------------------- #
def test_revision_seeds_prior_deliverable_as_target_evidence(tmp_path, monkeypatch):
    """The campaign shape: the prior deliverable was agent TEXT (no workspace
    file), so preserve-discipline criteria were unverifiable. A revision run
    must hold it as a retained, target-tier, citable file."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    prior = tmp_path / "runs" / "prev-run"
    (prior / "outputs").mkdir(parents=True)
    (prior / "workspace").mkdir()      # empty: text-only prior deliverable
    (prior / "outputs" / "DELIVERABLE.md").write_text("### Q: one\nanswer\n")

    tracer = RecordingTracer()

    async def runner(_t, name, prompt, options):
        return "revised"

    planner = StubPlanner([StageSpec(id="revise", pattern="single", role="E",
                                     goal="apply the feedback")],
                          revision={"of_run": "prev-run", "feedback": "new price",
                                    "preserve": "everything else"})
    orch, bb = _orch(tmp_path, runner, planner, "rev1", tracer)
    try:
        asyncio.run(orch.run("update the answer", seed_run="prev-run"))
    finally:
        bb.close()
    ws = tmp_path / "runs" / "rev1" / "workspace"
    seeded = ws / "prior" / "prev-run" / "outputs" / "DELIVERABLE.md"
    assert seeded.is_file() and "### Q: one" in seeded.read_text()
    assert "revision.deliverable_seeded" in tracer.kinds()
    entries = evidence.ledger(ws, run_dir=tmp_path / "runs" / "rev1")
    mine = [e for e in entries if e.file.endswith("outputs/DELIVERABLE.md")]
    assert mine and mine[0].tier == "target"
    assert mine[0].capability == "prior_deliverable"
# --- grounding: the corpus manifest makes bounded absence citable ------------------ #
def test_absence_claim_citing_corpus_manifest_survives_the_gate(tmp_path, monkeypatch):
    """The campaign shape: a sound 'no such document exists in the sources'
    claim was voided as a miscitation because the enumeration existed nowhere
    as a file. With the engine-written manifest, the same claim resolves —
    and the claims gate itself is untouched."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()

    async def runner(_t, name, prompt, options):
        if name == "acquire":
            run = tmp_path / "runs" / "absence1"
            ws = run / "workspace"
            for rel in ("reader/doc1.txt", "reader/doc2.txt"):
                p = ws / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("content")
            evidence.record(run, "reader", ["reader/doc1.txt", "reader/doc2.txt"],
                            ["https://example.test/pack"], "complete", workspace=ws)
            return "acquired"
        if name.startswith("check.r"):
            assert "CORPUS MANIFEST" in prompt
            return ("VERDICT: approved\n```claims\n"
                    + json.dumps([
                        {"claim": "what the sources state",
                         "support": "direct", "evidence": "reader/doc1.txt"},
                        {"claim": "no second volume exists in the sources",
                         "support": "negative",
                         "evidence": "corpus_manifest/manifest.md"},
                    ]) + "\n```")
        return "the account"

    planner = StubPlanner(
        [StageSpec(id="acquire", pattern="single", role="A",
                   goal="acquire the sources"),
         StageSpec(id="write", pattern="single", role="W", goal="write",
                   depends_on=["acquire"]),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["write"], rounds=1)])
    orch, bb = _orch(tmp_path, runner, planner, "absence1", tracer)
    try:
        asyncio.run(orch.run("Report on https://example.test/pack"))
    finally:
        bb.close()
    assert orch.classification == "succeeded"
    manifest = tmp_path / "runs" / "absence1" / "workspace" / "corpus_manifest" / "manifest.md"
    assert manifest.is_file()
    text = manifest.read_text()
    assert "reader/doc1.txt" in text and "reader/doc2.txt" in text
    assert "exactly 2 retained file(s)" in text
    # The engine's own manifest must not inflate the evidence metric.
    dims = next(d for k, _s, _st, d in tracer.events if k == "run.dimensions")
    assert dims["target_evidence"] == 2


def test_miscited_absence_claim_still_voids(tmp_path, monkeypatch):
    """The gate is NOT weakened: an absence claim citing something that is not
    a target-lineage file still voids the approval."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()

    async def runner(_t, name, prompt, options):
        if name == "acquire":
            run = tmp_path / "runs" / "absence2"
            ws = run / "workspace"
            p = ws / "reader" / "doc1.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("content")
            evidence.record(run, "reader", ["reader/doc1.txt"],
                            ["https://example.test/pack"], "complete", workspace=ws)
            return "acquired"
        if name.startswith("check.r"):
            return ("VERDICT: approved\n```claims\n"
                    + json.dumps([
                        {"claim": "what the sources state",
                         "support": "direct", "evidence": "reader/doc1.txt"},
                        {"claim": "no second volume exists",
                         "support": "negative",
                         "evidence": "the retained evidence ledger lists exactly one file"},
                    ]) + "\n```")
        return "the account"

    planner = StubPlanner(
        [StageSpec(id="acquire", pattern="single", role="A", goal="acquire"),
         StageSpec(id="write", pattern="single", role="W", goal="write",
                   depends_on=["acquire"]),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["write"], rounds=1)])
    orch, bb = _orch(tmp_path, runner, planner, "absence2", tracer)
    try:
        asyncio.run(orch.run("Report on https://example.test/pack"))
    finally:
        bb.close()
    assert "review.approval.voided" in tracer.kinds()
    assert not orch._review_approved


# --- perception: the authoring contract demands a guarded zero-yield path ---------- #
