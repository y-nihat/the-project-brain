"""Failure-driven recovery (V051-YouTube review): a method that cannot be
BUILT or cannot RUN is an observation about the strategy, not a run-killing
exception. These pin the previously unreachable boundary — capability
acquisition dying before any agent existed — flowing into the same
observe -> evaluate -> replan loop as agent-reported evidence gaps, plus the
evidence-contract, catalog-hygiene, and transactional-build guarantees around
it. Pure/stubbed parts — live behavior is validated in runs."""
from __future__ import annotations

import asyncio
import json

import pytest

from brain import acquisition, config
from brain.acquisition import AcquisitionError
from brain.memory.blackboard import Blackboard
from brain.models import CREATE, Capability, Decision, StageSpec
from brain.observability import NullTracer
from brain.orchestrator import Orchestrator
from brain.planner import StubPlanner
from brain.registry import Registry, qualified_catalog
from brain.sandbox.manager import RunResult


class CreateResolver:
    """Every capability need resolves to CREATE (nothing reusable exists)."""

    def resolve(self, need):
        return Decision(kind=CREATE, capability_need=need)


class RecoveringPlanner(StubPlanner):
    """StubPlanner with a scripted extension: what the strategy adjudicator
    would return when the failure re-enters planning."""

    def __init__(self, stages, extension, criteria=None, needs=None, ext_needs=None):
        super().__init__(stages, criteria, capability_needs=needs)
        self._ext = list(extension)
        self._ext_needs = dict(ext_needs or {})
        self.extend_calls: list[dict] = []

    async def extend(self, objective, completed, gaps, existing_ids):
        self.extend_calls.append({"completed": completed, "gaps": gaps,
                                  "existing_ids": list(existing_ids)})
        return ([s for s in self._ext if s.id not in set(existing_ids)],
                [], dict(self._ext_needs))


class RecordingTracer(NullTracer):
    def __init__(self):
        self.events: list[tuple] = []

    def event(self, kind, subject, status="info", **data):
        self.events.append((kind, subject, status, data))


def _fail_acquire(failure_class="packaging", attempts=3, history=None):
    async def acquire_stub(need, **kwargs):
        raise AcquisitionError(
            f"could not author a working '{need}': resource missing at runtime",
            need=need, failure_class=failure_class, attempts=attempts,
            history=history if history is not None else [
                "attempt 1: implementation — library API mismatch",
                "attempt 2: implementation — channel reported no content",
                "attempt 3: packaging — runtime resource not found",
            ])
    return acquire_stub


# --- P0-A/E/F: acquisition failure re-enters strategy adjudication ------------ #
def test_first_stage_acquisition_failure_triggers_recovery_plan(tmp_path, monkeypatch):
    """The observed terminal failure, fixed: authoring exhausts its attempts
    BEFORE any agent exists; the controller converts that into a residual
    requirement, the planner grafts an alternative method, the run delivers
    from the replacement — no agent ever had to call report_evidence_gap."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr("brain.orchestrator.acquire", _fail_acquire())
    bb = Blackboard(tmp_path / "b.db", "rec1")
    # Partial evidence from before the failure must survive and be surfaced.
    prev = tmp_path / "runs" / "rec1" / "workspace" / "earlier_probe"
    prev.mkdir(parents=True)
    (prev / "raw.txt").write_text("retained observation")

    async def runner(tracer, name, prompt, options):
        if name == "observe_alt":
            # Real retention shape: payload + provenance (the gate checks lineage).
            from brain import evidence
            d = tmp_path / "runs" / "rec1" / "workspace" / "alt_observer"
            d.mkdir(parents=True, exist_ok=True)
            (d / "obs.txt").write_text("A then B")
            evidence.record(tmp_path / "runs" / "rec1", "alt_observer",
                            ["alt_observer/obs.txt"], ["https://example.test/v/abc"],
                            "complete")
            return "observed via the alternative method: A then B"
        if name.startswith("review.r"):
            return ("VERDICT: approved\n```claims\n"
                    + json.dumps([{"claim": "the account of the source",
                                   "support": "direct",
                                   "evidence": "alt_observer/obs.txt"}]) + "\n```")
        raise AssertionError(f"unexpected session: {name}")

    planner = RecoveringPlanner(
        [StageSpec(id="summarize", pattern="single", role="S",
                   goal="observe the designated source", capabilities=["fetch_thing"])],
        [StageSpec(id="observe_alt", pattern="single", role="O",
                   goal="observe the source a materially different way")])
    orch = Orchestrator(session="rec1", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner,
                        resolver=CreateResolver(), session_runner=runner)
    try:
        answer = asyncio.run(orch.run(
            "Explain what happens in https://example.test/v/abc"))
        gaps, failures = bb.get("evidence.gaps"), bb.get("method.failures")
    finally:
        bb.close()
    # Recovery ran exactly once, from the classified failure — not from a gap.
    assert len(planner.extend_calls) == 1
    call = planner.extend_calls[0]
    assert gaps is None                               # no agent ever reported one
    attempted = call["gaps"][0]["attempted"]
    assert "packaging" in attempted and "3 attempt(s)" in attempted
    assert "API mismatch" in attempted                # per-attempt history escalates too
    assert "PARTIAL OBSERVATIONS" in call["completed"] and "raw.txt" in call["completed"]
    # The failure is durable controller state.
    assert failures[0]["failure_class"] == "packaging"
    # The replacement became the artifact-of-record: review retargeted to it.
    review = orch._stages_by_id["review"]
    assert review.depends_on[0] == "observe_alt"
    assert "summarize" not in orch._outputs            # failed node produced nothing
    assert "alternative method" in answer
    assert orch.classification == "succeeded"


def test_recovery_replaces_failed_node_and_rewires_dependents(tmp_path, monkeypatch):
    """A mid-graph node that never starts is replaced transactionally: its
    dependents' upstream becomes the replacement terminal, and the failed node
    survives only as failure history — never as evidence."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr("brain.orchestrator.acquire", _fail_acquire())
    bb = Blackboard(tmp_path / "b.db", "rec2")

    ran: list[str] = []

    async def runner(tracer, name, prompt, options):
        ran.append(name)
        if name == "conclude":
            assert "replacement notes" in prompt      # rewired upstream flowed in
        return {"gather": "gathered", "replace": "replacement notes",
                "conclude": "final"}.get(name, f"out({name})")

    planner = RecoveringPlanner(
        [StageSpec(id="gather", pattern="single", role="G", goal="g"),
         StageSpec(id="broken", pattern="single", role="B", goal="needs a tool",
                   capabilities=["fetch_thing"], depends_on=["gather"]),
         StageSpec(id="conclude", pattern="single", role="C", goal="conclude",
                   depends_on=["broken"])],
        [StageSpec(id="replace", pattern="single", role="R",
                   goal="establish it differently", depends_on=["gather"])])
    orch = Orchestrator(session="rec2", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner,
                        resolver=CreateResolver(), session_runner=runner)
    try:
        answer = asyncio.run(orch.run("do the thing"))
    finally:
        bb.close()
    assert ran == ["gather", "replace", "conclude"]
    assert orch._stages_by_id["conclude"].depends_on == ["replace"]
    assert "broken" not in orch._outputs
    assert answer.startswith("final")
    assert orch.classification == "succeeded"


def test_calibrated_stop_finalizes_dimensions_not_traceback(tmp_path, monkeypatch):
    """When the adjudicator finds NO admissible alternative, the run still
    terminates through the authoritative state machine: run.dimensions and
    run.classification exist, the user gets a controller diagnosis (what was
    tried / what remains / why stopped), no DELIVERABLE.md pretends, and no
    raw traceback is the answer."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr("brain.orchestrator.acquire", _fail_acquire())
    bb = Blackboard(tmp_path / "b.db", "rec3")
    tracer = RecordingTracer()

    async def runner(tracer_, name, prompt, options):
        raise AssertionError("no agent session may start")

    planner = RecoveringPlanner(
        [StageSpec(id="only", pattern="single", role="S", goal="observe the source",
                   capabilities=["fetch_thing"])],
        extension=[])                                  # nothing admissible remains
    orch = Orchestrator(session="rec3", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner,
                        resolver=CreateResolver(), session_runner=runner, tracer=tracer)
    try:
        answer = asyncio.run(orch.run("observe the thing"))   # must NOT raise
    finally:
        bb.close()
    assert orch.classification == "failed"
    assert "What was attempted" in answer and "Why execution stopped" in answer
    assert "no admissible alternative" in answer
    assert "Traceback" not in answer
    dims = next(d for k, s, st, d in tracer.events if k == "run.dimensions")
    assert dims["deliverable"] == "absent"
    assert dims["stage_completion"] == "none-completed"
    assert "no admissible" in dims["recovery"]
    assert any(k == "run.classification" and d.get("classification") == "failed"
               for k, s, st, d in tracer.events)
    outputs = tmp_path / "runs" / "rec3" / "outputs"
    assert not (outputs / "DELIVERABLE.md").exists()   # absent, not a lying stub
    assert "What was attempted" in (outputs / "ANSWER.md").read_text()


def test_recovery_budget_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "RECOVERIES_PER_RUN", 0)
    monkeypatch.setattr("brain.orchestrator.acquire", _fail_acquire())
    bb = Blackboard(tmp_path / "b.db", "rec4")

    async def runner(tracer, name, prompt, options):
        return "unreachable"

    planner = RecoveringPlanner(
        [StageSpec(id="only", pattern="single", role="S", goal="g",
                   capabilities=["fetch_thing"])],
        [StageSpec(id="alt", pattern="single", role="A", goal="g")])
    orch = Orchestrator(session="rec4", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner,
                        resolver=CreateResolver(), session_runner=runner)
    try:
        answer = asyncio.run(orch.run("objective"))
    finally:
        bb.close()
    assert planner.extend_calls == []                  # budget consulted first
    assert orch.classification == "failed"
    assert "budget exhausted" in answer


def test_suppressed_method_needs_a_new_contract_to_retry(tmp_path, monkeypatch):
    """Blind re-selection of the failed method is refused (that is the
    repaired-forever loop); a recovery that re-names the same key WITHOUT a new
    capability contract fails as 'suppressed' without burning another authoring
    session. A fresh contract lifts the suppression."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    calls = {"n": 0}

    async def acquire_once_then_never(need, **kwargs):
        calls["n"] += 1
        raise AcquisitionError("nope", need=need, failure_class="implementation",
                               attempts=3)
    monkeypatch.setattr("brain.orchestrator.acquire", acquire_once_then_never)
    bb = Blackboard(tmp_path / "b.db", "rec5")

    async def runner(tracer, name, prompt, options):
        return "out"

    planner = RecoveringPlanner(
        [StageSpec(id="only", pattern="single", role="S", goal="g",
                   capabilities=["fetch_thing"])],
        [StageSpec(id="again", pattern="single", role="A", goal="same method again",
                   capabilities=["fetch_thing"])])     # same key, NO new contract
    orch = Orchestrator(session="rec5", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner,
                        resolver=CreateResolver(), session_runner=runner)
    try:
        asyncio.run(orch.run("objective"))
        classes = [f["failure_class"] for f in bb.get("method.failures")]
    finally:
        bb.close()
    assert calls["n"] == 1                             # suppression, not re-authoring
    assert classes == ["implementation", "suppressed"]
    assert orch.classification == "failed"


# --- P0-B: the developer sees a contract, never the raw stage goal ------------- #
def test_capability_contract_reaches_developer_not_stage_goal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    seen: list[dict] = []

    async def capture_acquire(need, **kwargs):
        seen.append({"need": need, "description": kwargs.get("description"),
                     "example_input": kwargs.get("example_input")})
        return Capability(name=need, description="d", image_tag="i:1", command=["x"],
                          input_schema={"type": "object", "properties": {}}, env_map={})
    monkeypatch.setattr("brain.orchestrator.acquire", capture_acquire)
    bb = Blackboard(tmp_path / "b.db", "need1")

    async def runner(tracer, name, prompt, options):
        return "ok"

    goal = ("Fetch the full text of https://example.test/v/abc?ref=x and write a single "
            "paragraph (no more than ten sentences) describing it")
    planner = StubPlanner(
        [StageSpec(id="s", pattern="single", role="Observer", goal=goal,
                   capabilities=["fetch_page_text"])],
        capability_needs={"fetch_page_text": {
            "contract": "Retrieve the primary textual content available for a remotely "
                        "hosted source given its locator; report coverage per requested "
                        "property and unresolved needs honestly",
            "example_input": {"url": "https://example.test/v/abc?ref=x"}}})
    orch = Orchestrator(session="need1", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner,
                        resolver=CreateResolver(), session_runner=runner)
    try:
        asyncio.run(orch.run(goal))
    finally:
        bb.close()
    d = seen[0]
    assert d["description"].startswith("Retrieve the primary textual content")
    assert "example.test" not in d["description"] and "ten sentences" not in d["description"]
    # The concrete instance rides SEPARATELY, for verification only.
    assert d["example_input"] == {"url": "https://example.test/v/abc?ref=x"}


def test_fallback_authoring_description_is_scrubbed_of_task_policy(tmp_path, monkeypatch):
    """Even with no planner-compiled contract, the stage goal is generalized
    before it reaches the developer — a URL and a ten-sentence policy were
    observed compiled into three generations of an unusable monolith."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    seen: list[str] = []

    async def capture_acquire(need, **kwargs):
        seen.append(kwargs.get("description"))
        return Capability(name=need, description="d", image_tag="i:1", command=["x"],
                          input_schema={"type": "object", "properties": {}}, env_map={})
    monkeypatch.setattr("brain.orchestrator.acquire", capture_acquire)
    bb = Blackboard(tmp_path / "b.db", "need2")

    async def runner(tracer, name, prompt, options):
        return "ok"

    goal = ("Fetch the primary content and metadata of the source at "
            "https://example.test/v/abc123?ref=xyz, then write a single paragraph "
            "(maximum 10 sentences) describing what happens in it")
    planner = StubPlanner([StageSpec(id="s", pattern="single", role="Fetcher",
                                     goal=goal, capabilities=["fetch_source_text"])])
    orch = Orchestrator(session="need2", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner,
                        resolver=CreateResolver(), session_runner=runner)
    try:
        asyncio.run(orch.run(goal))
    finally:
        bb.close()
    assert "example.test" not in seen[0] and "https://" not in seen[0]
    assert "10" not in seen[0] and "single paragraph" not in seen[0]


# --- P0-C/D: operational verification vs evidence yield ------------------------ #
def _obs_cap(network="bridge"):
    return Capability(name="obs", description="d", image_tag="i:1", command=["x"],
                      input_schema={"type": "object", "properties": {}}, env_map={},
                      network=network, emits="observation")


def _res(stdout, files=None, exit_code=0):
    return RunResult(container_id="c", exit_code=exit_code, stdout=stdout, stderr="",
                     timed_out=False, output_files=files or {})


class OneShotManager:
    def __init__(self, result):
        self._result = result

    def run_ephemeral(self, *a, **kw):
        return self._result


def test_negative_observation_yield_is_operational_success():
    """'The requested property is not retrievable' is a VALID observation:
    exit 0, coverage 'none', the unresolved property named — never a broken
    tool."""
    out = json.dumps({"coverage": "none", "unresolved_needs": ["the requested property"],
                      "observation_state": "observed_absent",
                      "source_refs": [], "consumed_inputs": []})
    ok, detail, _ = acquisition._generic_verify(_obs_cap(), {}, {}, OneShotManager(_res(out)))
    assert ok, detail


def test_adjacent_channel_cannot_claim_target_coverage():
    """coverage='complete' with named unresolved needs is the substitution
    signature (length-based coverage, description relabeled as content) —
    rejected deterministically."""
    out = json.dumps({"coverage": "complete",
                      "unresolved_needs": ["the requested property itself"],
                      "source_refs": ["https://x"], "consumed_inputs": []})
    ok, detail, _ = acquisition._generic_verify(
        _obs_cap(), {}, {}, OneShotManager(_res(out, files={"description.txt": b"x"})))
    assert not ok and "CONTRADICTORY YIELD" in detail


def test_observation_role_requires_yield_fields():
    ok, detail, _ = acquisition._generic_verify(
        _obs_cap(), {}, {}, OneShotManager(_res(json.dumps({"data": "x"}))))
    assert not ok and "MISSING EVIDENTIARY YIELD" in detail


def test_remote_positive_yield_requires_lineage_and_retention():
    # Positive yield with no source_refs -> rejected.
    out1 = json.dumps({"coverage": "complete", "unresolved_needs": [],
                       "observation_state": "observed_present"})
    ok1, d1, _ = acquisition._generic_verify(
        _obs_cap(), {}, {}, OneShotManager(_res(out1, files={"payload.txt": b"x"})))
    assert not ok1 and "MISSING SOURCE LINEAGE" in d1
    # Positive yield with no retained raw payload -> rejected.
    out2 = json.dumps({"coverage": "partial", "unresolved_needs": ["rest"],
                       "observation_state": "observed_present",
                       "source_refs": ["https://x"]})
    ok2, d2, _ = acquisition._generic_verify(_obs_cap(), {}, {}, OneShotManager(_res(out2)))
    assert not ok2 and "UNRETAINED OBSERVATION" in d2
    # A LOCAL observer (no network) is not held to remote retention rules.
    out3 = json.dumps({"coverage": "complete", "unresolved_needs": [],
                       "observation_state": "observed_present"})
    ok3, d3, _ = acquisition._generic_verify(
        _obs_cap(network="none"), {}, {}, OneShotManager(_res(out3)))
    assert ok3, d3


def test_developer_contract_teaches_negative_yield_lineage_and_build_asserts():
    c = acquisition._DEVELOPER_SYSTEM
    assert "NEGATIVE OBSERVATION IS A VALID RESULT" in c
    assert "CHANNEL LINEAGE" in c and "a schema is not current state" in c
    assert "DOWNLOADS MUST FAIL THE BUILD" in c
    assert "heavyweight interpretation/summarization dependency to an observer" in c
    # The contract teaches GENERAL rules: no task/domain of any diagnostic
    # trial may leak into what every future developer agent reads.
    for residue in ("youtube", "youtu.be", "video", "transcript", "caption",
                    "nltk", "punkt", "sumy"):
        assert residue not in c.lower(), f"trial residue {residue!r} in developer contract"


# --- P0-D4 + P1-C: authoring-time contract + transactional promotion ----------- #
class ScriptedManager:
    """Fake sandbox manager: records image operations, scripts verify results."""

    def __init__(self, verify_results):
        self.built: list[str] = []
        self.tagged: list[tuple] = []
        self.removed: list[str] = []
        self._verify = list(verify_results)
        self._i = 0

    def build_image(self, tag, ctx):
        self.built.append(tag)

    def tag_image(self, source, target):
        self.tagged.append((source, target))

    def remove_image(self, tag):
        self.removed.append(tag)

    def run_ephemeral(self, image, command, *, environment=None, **kw):
        if not environment:            # fabrication probe: honest empty failure
            return _res(json.dumps({"error": "no input provided"}), exit_code=2)
        r = self._verify[min(self._i, len(self._verify) - 1)]
        self._i += 1
        return r


def test_remote_authoring_requires_full_evidence_contract(tmp_path):
    """A network observer is held to the complete evidence contract at INITIAL
    authoring — missing `emits` and missing yield fields are fed back as
    contract errors; only a contract-complete submission promotes."""
    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    feedback: list[str] = []
    subs = iter([
        {"network": "bridge"},                                     # no emits
        {"network": "bridge", "emits": "observation",
         "stdout": json.dumps({"data": "adjacent text"})},         # no yield fields
        {"network": "bridge", "emits": "observation",
         "stdout": json.dumps({"coverage": "none",
                               "unresolved_needs": ["target content"],
                               "source_refs": []})},               # honest negative
    ])

    async def dev(need_key, description, previous=None, error=None):
        if error:
            feedback.append(error)
        s = next(subs)
        return {"program": "p", "dockerfile": "FROM python:3.12-slim",
                "command": ["python", "-B", "/app/tool.py"],
                "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
                "env_map": {"q": "Q"}, "network": s["network"],
                "sample_input": {"q": "probe"}, "emits": s.get("emits"),
                "_stdout": None}

    mgr = ScriptedManager([
        _res(json.dumps({"data": "adjacent text"})),               # attempt 2 verify
        _res(json.dumps({"coverage": "none", "unresolved_needs": ["target content"],
                         "observation_state": "not_detected",
                         "source_refs": []})),                     # attempt 3 verify
    ])
    cap = asyncio.run(acquisition._author_capability(
        "fetch_remote_thing", "observe a remote source", manager=mgr, registry=reg,
        broker=None, model=None, developer=dev, max_attempts=3, tracer=NullTracer()))
    reg.close()
    assert cap.verified
    assert any("`emits`" in f for f in feedback)
    assert any("MISSING EVIDENTIARY YIELD" in f for f in feedback)


def test_candidate_image_promotes_atomically(tmp_path):
    """An unverified attempt must NEVER hold the canonical tag: candidates are
    attempt-scoped; only the verified one is retagged (all three failed YouTube
    attempts were observed overwriting `brain/cap-...:1.0.0` in place)."""
    reg = Registry(tmp_path / "r.db", tmp_path / "store")

    async def dev(need_key, description, previous=None, error=None):
        return {"program": "p", "dockerfile": "FROM python:3.12-slim",
                "command": ["python", "-B", "/app/tool.py"],
                "input_schema": {"type": "object", "properties": {}},
                "env_map": {"q": "Q"}, "network": "none",
                "sample_input": {"q": "x"}}

    mgr = ScriptedManager([
        _res(json.dumps({"error": "broken"}), exit_code=1),        # attempt 1 fails
        _res(json.dumps({"ok": True, "consumed_inputs": []})),     # attempt 2 passes
    ])
    cap = asyncio.run(acquisition._author_capability(
        "atomic_probe", "a tool", manager=mgr, registry=reg, broker=None,
        model=None, developer=dev, max_attempts=3, tracer=NullTracer()))
    reg.close()
    canonical = "brain/cap-atomic_probe:1.0.0"
    assert cap.verified and cap.image_tag == canonical
    assert canonical not in mgr.built                  # never built in place
    assert all(":cand-" in t for t in mgr.built) and len(mgr.built) == 2
    assert len(mgr.tagged) == 1 and mgr.tagged[0][1] == canonical
    assert mgr.built[0] in mgr.removed                 # failed candidate cleaned up


def test_authoring_failure_is_classified_and_remembered(tmp_path):
    """Exhausted authoring raises CLASSIFIED state (class, attempts, per-attempt
    history) and records scoped negative memory in the registry."""
    reg = Registry(tmp_path / "r.db", tmp_path / "store")

    async def dev(need_key, description, previous=None, error=None):
        return {"program": "p", "dockerfile": "FROM python:3.12-slim",
                "command": ["python", "-B", "/app/tool.py"],
                "input_schema": {"type": "object", "properties": {}},
                "env_map": {"q": "Q"}, "network": "none", "sample_input": {"q": "x"}}

    mgr = ScriptedManager([_res(json.dumps({"error": "still broken"}), exit_code=1)])
    with pytest.raises(AcquisitionError) as ei:
        asyncio.run(acquisition._author_capability(
            "doomed_probe", "a tool", manager=mgr, registry=reg, broker=None,
            model=None, developer=dev, max_attempts=2, tracer=NullTracer()))
    err = ei.value
    assert err.failure_class == "implementation" and err.attempts == 2
    assert len(err.history) == 2 and "implementation" in err.history[0]
    remembered = reg.last_failures()
    reg.close()
    assert "doomed_probe" in remembered
    assert remembered["doomed_probe"]["class"] == "implementation"


# --- P1-B: catalog hygiene before planning -------------------------------------- #
def test_catalog_qualifies_quarantined_stale_and_failed(tmp_path):
    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    epoch = acquisition.validation_epoch()

    def cap(name, desc, validated=None):
        return Capability(name=name, description=desc, image_tag="i:1", command=["x"],
                          input_schema={"type": "object", "properties": {}}, env_map={},
                          verified=True, validated_engine=validated, emits="observation")

    reg.add(cap("current_tool", "Extract text from a provided file.", validated=epoch))
    reg.add(cap("stale_tool", "Fetch the full text of https://example.test/v/abc and "
                              "write a single paragraph (maximum 10 sentences).",
                validated="0.0.1+c0"))
    reg.add(cap("bad_tool", "does bad things", validated=epoch))
    reg.quarantine("bad_tool", "fabricates evidence")
    reg.record_failure("ghost_fetch", "no such channel exposed by the source",
                       "implementation")

    known = qualified_catalog(reg, epoch)
    reg.close()
    assert "bad_tool" not in known                     # unsafe memory not advertised
    assert known["current_tool"] == "Extract text from a provided file."
    assert "[stale contract" in known["stale_tool"]
    assert "example.test" not in known["stale_tool"]   # residue normalized in the view
    assert "maximum 10" not in known["stale_tool"]
    assert "UNAVAILABLE" in known["ghost_fetch"]       # dead ends carry their history
    assert "materially different" in known["ghost_fetch"]


# --- P0-F: the initial strategy is auditable state ------------------------------ #
def test_initial_strategy_rationale_is_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    bb = Blackboard(tmp_path / "b.db", "rat")

    async def runner(tracer, name, prompt, options):
        return "fine"

    planner = StubPlanner(
        [StageSpec(id="s", pattern="single", role="R", goal="g")],
        strategy_rationale="direct observation of the source beats metadata: the "
                           "objective asks about content and event order")
    orch = Orchestrator(session="rat", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner)
    try:
        asyncio.run(orch.run("describe the thing"))
        assert "direct observation" in bb.get("plan.rationale")
    finally:
        bb.close()


def test_planner_prompts_demand_adjudication_contracts_and_calibrated_stop():
    from brain import planner as planner_mod
    s = planner_mod._SYSTEM
    assert "STRATEGY ADJUDICATION" in s and "EPISTEMIC FIT FIRST" in s
    assert "ATOMIC CAPABILITIES" in s and "fetch-and-summarize monolith" in s
    assert "CAPABILITY NEEDS" in s and "INSTANCE-NEUTRAL" in s
    e = planner_mod._EXTEND_ADDENDUM
    assert "FAILED METHODS ARE EVIDENCE" in e and "calibrated stop" in e
