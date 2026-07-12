"""The adaptive control slice (V042-YouTube review): evidence-gap replanning,
honest classification states, remote sources as first-class evidence, and
memory/consolidation hygiene. Pure/stubbed parts — live behavior is validated
in runs."""
from __future__ import annotations

import asyncio

import pytest

from brain import acquisition, config, workflow
from brain.memory.blackboard import Blackboard
from brain.models import Capability, StageSpec
from brain.orchestrator import _EXTERNAL_LOCATOR, Orchestrator
from brain.planner import StubPlanner


# --- review trigger: a remote locator is user-referenced evidence ------------- #
def test_external_locator_detection():
    assert _EXTERNAL_LOCATOR.search("summarize https://example.test/v/abc please")
    assert _EXTERNAL_LOCATOR.search("see www.example.test/doc")
    assert _EXTERNAL_LOCATOR.search("s3://bucket/key.bin")
    assert not _EXTERNAL_LOCATOR.search("summarize the attached file about cats")


def test_url_objective_forces_grounding_review():
    stages = [StageSpec(id="s", pattern="single", role="r", goal="summarize a source")]
    out, appended = workflow.append_review_if_needed(
        stages, has_user_evidence=False, has_external_evidence=True)
    assert appended and out[-1].pattern == "review"
    # No evidence referenced at all -> unchanged behavior.
    out2, appended2 = workflow.append_review_if_needed(
        stages, has_user_evidence=False, has_external_evidence=False)
    assert not appended2


# --- team channel: gaps and blocker severity are controller state ------------- #
def _channel(tmp_path):
    from brain.team import TeamChannel

    bb = Blackboard(tmp_path / "bb.db", "adaptive")

    async def ask(stage, q):
        return "answer"

    return TeamChannel(agent_id="w1", completed_stages=[], ask_fn=ask, blackboard=bb), bb


def test_evidence_gap_recorded_with_anti_fabrication_guidance(tmp_path):
    ch, bb = _channel(tmp_path)
    try:
        text = ch.evidence_gap("the source's ordered events", attempted="metadata retrieval")
        assert "recorded" in text and "do NOT fill it from memory" in text
        gap = bb.get("evidence.gaps")[0]
        assert gap["need"] == "the source's ordered events"
        assert gap["attempted"] == "metadata retrieval"
    finally:
        bb.close()


def test_fatal_blocker_is_distinct_state(tmp_path):
    ch, bb = _channel(tmp_path)
    try:
        text = ch.block("the source does not exist", severity="fatal")
        assert "final status" in text
        assert bb.get("team.blockers")[0]["severity"] == "fatal"
        # Unknown severities degrade to recoverable, never crash.
        ch.block("minor", severity="whatever")
        assert bb.get("team.blockers")[1]["severity"] == "recoverable"
    finally:
        bb.close()


# --- the loop re-enters on an unmet observation need -------------------------- #
class ExtendingPlanner(StubPlanner):
    def __init__(self, stages, extension, criteria=None):
        super().__init__(stages, criteria)
        self._ext = list(extension)
        self.extend_calls = 0

    async def extend(self, objective, completed, gaps, existing_ids):
        self.extend_calls += 1
        self.last_gaps = gaps
        return [s for s in self._ext if s.id not in set(existing_ids)]


def test_evidence_gap_triggers_plan_extension(tmp_path, monkeypatch):
    """'transcript unavailable' must change the STRATEGY, not just the data:
    a reported gap re-enters planning, the extension stage runs, and its
    output becomes the deliverable."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    bb = Blackboard(tmp_path / "b.db", "ext")

    async def runner(tracer, name, prompt, options):
        if name == "fetch":
            # Simulates the agent calling report_evidence_gap mid-session.
            bb.put("evidence.gaps", [{"agent": "fetch", "need": "the events themselves",
                                      "attempted": "metadata retrieval"}])
            return "identity and metadata only"
        if name == "observe":
            return "observed: the actual event sequence"
        raise AssertionError(name)

    planner = ExtendingPlanner(
        [StageSpec(id="fetch", pattern="single", role="F", goal="observe the source")],
        [StageSpec(id="observe", pattern="single", role="O",
                   goal="observe what fetch could not", depends_on=["fetch"])])
    orch = Orchestrator(session="ext", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner)
    try:
        answer = asyncio.run(orch.run("describe what happens in the thing"))
    finally:
        bb.close()
    assert planner.extend_calls == 1
    assert planner.last_gaps[0]["need"] == "the events themselves"
    assert "actual event sequence" in answer


def test_replanning_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    bb = Blackboard(tmp_path / "b.db", "bound")

    async def runner(tracer, name, prompt, options):
        # EVERY stage reports a fresh gap — the loop must not spin.
        gaps = bb.get("evidence.gaps", []) or []
        bb.put("evidence.gaps", gaps + [{"agent": name, "need": f"more from {name}"}])
        return f"{name} done"

    planner = ExtendingPlanner(
        [StageSpec(id="a", pattern="single", role="A", goal="g")],
        [StageSpec(id="b", pattern="single", role="B", goal="g", depends_on=["a"])])
    orch = Orchestrator(session="bound", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner)
    try:
        asyncio.run(orch.run("objective"))
    finally:
        bb.close()
    assert planner.extend_calls == config.REPLANS_PER_RUN == 1


def test_extension_lands_before_pending_review_and_feeds_it(tmp_path, monkeypatch):
    """End-to-end shape of the observed failure, fixed: a URL objective gets an
    engine-appended review; the draft stage reports a gap; the extension runs
    BEFORE the review; the review sees its notes and approves; the run is
    honestly SUCCEEDED via an actual assessment. The gather stage RETAINS a
    payload — on a source-derived run, approval over an empty evidence ledger
    is voided by the engine (V060), so an honest SUCCEEDED requires retention."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    bb = Blackboard(tmp_path / "b.db", "rev")

    ran: list[str] = []

    async def runner(tracer, name, prompt, options):
        ran.append(name)
        if name == "draft":
            bb.put("evidence.gaps", [{"agent": "draft", "need": "content, not metadata"}])
            return "metadata-only draft"
        if name == "gather":
            obs = tmp_path / "runs" / "rev" / "workspace" / "observer"
            obs.mkdir(parents=True, exist_ok=True)
            (obs / "content.txt").write_text("observed content: A then B then C")
            return "observed content: A then B then C"
        if name.startswith("review.r"):
            return "VERDICT: approved"
        return f"{name} ok"

    planner = ExtendingPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="summarize the source")],
        [StageSpec(id="gather", pattern="single", role="G",
                   goal="observe the missing content", depends_on=["draft"])],
        criteria=["the summary describes the source's actual content"])
    orch = Orchestrator(session="rev", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner)
    try:
        asyncio.run(orch.run("Summarize what happens in https://example.test/v/abc"))
    finally:
        bb.close()
    assert planner.extend_calls == 1
    assert ran.index("gather") < ran.index("review.r1")   # extension precedes review
    # Graph reconciliation: the extension's terminal stage is the new
    # artifact-of-record — review must treat IT as the draft it verifies and
    # repairs (deps[0]), with the superseded draft demoted to evidence.
    # Observed failure: repair reran the original stage while the extension's
    # synthesis sat unreviewable as evidence-only.
    review = orch._stages_by_id["review"]
    assert review.depends_on[0] == "gather"
    assert "draft" in review.depends_on[1:]
    assert orch.classification == "succeeded"


# --- classification honesty ---------------------------------------------------- #
def test_success_reason_never_asserts_unassessed_criteria(tmp_path, monkeypatch):
    """'grounded and criteria-consistent' shipped on a run where neither was
    ever evaluated. Unassessed states must read as unassessed."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    bb = Blackboard(tmp_path / "b.db", "honest")

    async def runner(tracer, name, prompt, options):
        return "a fine result"

    planner = StubPlanner([StageSpec(id="s", pattern="single", role="R", goal="g")],
                          success_criteria=["exactly one paragraph"])
    orch = Orchestrator(session="honest", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner)
    try:
        answer = asyncio.run(orch.run("write something nice"))  # no evidence referenced
    finally:
        bb.close()
    assert orch.classification == "succeeded"
    assert "criteria-consistent" not in answer
    # The honest reason is only prepended for non-success; check the trace state.


def test_url_objective_without_approval_is_unverified(tmp_path, monkeypatch):
    """A one-stage summary of a linked source can no longer ship as grounded
    when nothing checked its claims: the engine appends review, and a
    non-approving review yields UNVERIFIED. With an EMPTY evidence plane on a
    high-risk run, the reviewer is barred from approving outright (observed:
    16 empty lookups, then two rounds reasoned over upstream narration)."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    bb = Blackboard(tmp_path / "b.db", "url")
    review_prompts: list[str] = []

    async def runner(tracer, name, prompt, options):
        if name.startswith("review.r"):
            review_prompts.append(prompt)
            return "VERDICT: revise\n1. claims not traceable to the source"
        return "summary from metadata"

    planner = StubPlanner([StageSpec(id="sum", pattern="single", role="S", goal="summarize")])
    orch = Orchestrator(session="url", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner)
    try:
        answer = asyncio.run(orch.run("Explain what happens in https://example.test/v/xyz"))
    finally:
        bb.close()
    assert orch.classification == "unverified"
    assert "UNVERIFIED" in answer
    # No retained evidence + user-referenced remote source => approval barred.
    assert "NO ORIGINAL SOURCE EVIDENCE" in review_prompts[0]
    # Pure deliverable channel: exactly the stage output, no banner/appendix.
    pure = (tmp_path / "runs" / "url" / "outputs" / "DELIVERABLE.md").read_text()
    assert "UNVERIFIED" not in pure and "engine audit" not in pure


def test_extension_can_recompile_mechanism_bound_criteria(tmp_path, monkeypatch):
    """A criterion that froze the failed mechanism ('traceable to the
    transcript') must be revisable when the strategy changes — the reviewers
    were observed stuck with a contract incompatible with the objective."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    bb = Blackboard(tmp_path / "b.db", "recompile")

    class RecompilingPlanner(ExtendingPlanner):
        async def extend(self, objective, completed, gaps, existing_ids):
            stages = await super().extend(objective, completed, gaps, existing_ids)
            return stages, ["claims traceable to observations of the source "
                            "(proxy-derived claims carry that limitation)"]

    async def runner(tracer, name, prompt, options):
        if name == "a":
            bb.put("evidence.gaps", [{"agent": "a", "need": "actual content"}])
        return f"{name} out"

    planner = RecompilingPlanner(
        [StageSpec(id="a", pattern="single", role="A", goal="g")],
        [StageSpec(id="b", pattern="single", role="B", goal="g", depends_on=["a"])],
        criteria=["every claim traceable to the transcript retrieved this run"])
    orch = Orchestrator(session="recompile", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner)
    try:
        asyncio.run(orch.run("describe the thing"))
    finally:
        bb.close()
    assert orch._criteria == ["claims traceable to observations of the source "
                              "(proxy-derived claims carry that limitation)"]


def test_revalidation_rejects_pre_contract_remote_observer(tmp_path):
    """Network observers escaped the staleness gate (only file consumers were
    checked): two remote tools were re-stamped under a new epoch declaring no
    output role — the evidence plane failed open for their whole class."""
    from brain.registry import Registry

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    legacy = Capability(name="net_observer", description="d", image_tag="i:1",
                        command=["x"], input_schema={"type": "object", "properties": {}},
                        env_map={"q": "Q"}, network="bridge", verified=True)
    reg.add(legacy)
    ok, reason = acquisition.revalidate_capability(reg.get("net_observer"), None, reg)
    assert not ok and "older capability contract" in reason
    # A local pure generator (no files, no network) is not an observer at all.
    gen = Capability(name="gen", description="d", image_tag="i:1", command=["x"],
                     input_schema={"type": "object", "properties": {}}, env_map={},
                     network="none", verified=True)
    assert acquisition._declares_inputs(gen, {}) is False
    reg.close()


def test_fatal_blocker_prevents_succeeded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    bb = Blackboard(tmp_path / "b.db", "fatal")

    async def runner(tracer, name, prompt, options):
        bb.put("team.blockers", [{"agent": name, "issue": "the source cannot be observed",
                                  "needed": "", "severity": "fatal"}])
        return "best effort text"

    planner = StubPlanner([StageSpec(id="s", pattern="single", role="R", goal="g")])
    orch = Orchestrator(session="fatal", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner)
    try:
        answer = asyncio.run(orch.run("do the thing"))
    finally:
        bb.close()
    assert orch.classification == "blocked"
    assert "cannot be observed" in answer


# --- remote sources are first-class evidence ----------------------------------- #
def test_provenance_reports_remote_observation():
    from brain.capabilities.runtime import provenance_line
    line = provenance_line("fetcher", None,
                           {"source_refs": ["https://example.test/v/abc"],
                            "consumed_inputs": []})
    assert "observed by 'fetcher' from remote source(s)" in line
    assert "example.test" in line
    # No source_refs and no files -> unchanged honest wording.
    line2 = provenance_line("gen", None, {"data": 1})
    assert "from arguments only" in line2


def test_developer_contract_demands_yield_and_retained_remote_evidence():
    c = acquisition._DEVELOPER_SYSTEM
    assert "EVIDENTIARY YIELD" in c and "`coverage`" in c and "`unresolved_needs`" in c
    assert "REMOTE OBSERVATION" in c and "`source_refs`" in c
    assert "verbatim payloads" in c


# --- memory hygiene ------------------------------------------------------------- #
def test_prior_answers_are_labeled_hypothesis_not_evidence(tmp_path):
    from brain.memory import history
    run_dir = tmp_path / "runs" / "run-1" / "outputs"
    run_dir.mkdir(parents=True)
    (run_dir / "ANSWER.md").write_text("The film is about a toad on a bicycle.")
    block = history.run_context(tmp_path / "no.db", tmp_path / "runs",
                                {"run_id": "run-1", "status": "succeeded",
                                 "objective": "summarize the film"})
    assert "NOT evidence" in block and "hypothesis" in block
    assert "toad on a bicycle" in block          # the content itself still flows


# --- consolidation hygiene ------------------------------------------------------ #
def test_generalize_description_strips_locator_and_output_policy_residue():
    d = acquisition.generalize_description(
        "YouTube Video Summarizer: Fetch the transcript and/or metadata of the video at "
        "https://youtu.be/abc123?si=xyz, then write a single paragraph (maximum 10 "
        "sentences) describing what happens in it.")
    assert "youtu.be" not in d and "https://" not in d
    assert "a provided source" in d
    assert "10" not in d and "maximum" not in d.lower()
    assert "single paragraph" not in d
    d2 = acquisition.generalize_description("Search the live web via a keyless API.")
    assert d2 == "Search the live web via a keyless API."