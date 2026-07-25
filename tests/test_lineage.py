"""The trustworthy-certificate core (V0100 review, Phase A): lineage that flows
through transformations, the claim-support gate, standardized observation
states, deeper research, and strong-tier persistence. The failure these pin: a
run derived its answer from genuine property-preserving transformations of the
designated target, yet its formal ledger held only proxy items and approval
passed on mere non-emptiness — a hollow certificate over a real success; and an
extraction FAILURE was indistinguishable from a demonstrated absence."""
from __future__ import annotations

import asyncio
import json

from brain import acquisition, config, evidence
from brain.memory.blackboard import Blackboard
from brain.models import StageSpec
from brain.observability import NullTracer
from brain.orchestrator import Orchestrator, _claims_gate, _parse_claims
from brain.planner import StubPlanner
from brain.supervisor import Supervisor


# --- lineage propagates through transformations --------------------------------- #
def _ws(tmp_path, *files):
    ws = tmp_path / "run" / "workspace"
    for rel in files:
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"content of " + rel.encode())
    return ws


def test_lineage_flows_through_transformations(tmp_path):
    """The core rule: a file produced by a call that CONSUMED target-lineage
    inputs is target lineage ('derived'), whatever role its tool declared —
    and a chain of transformations preserves it (fetch -> convert -> convert)."""
    ws = _ws(tmp_path, "fetcher/payload.bin", "converter/part1.txt",
             "refiner/final.txt", "searcher/results.json", "writer/deck.html")
    run = ws.parent
    loc = ["https://example.test/v/abc"]
    # fetch: accessed the designated source directly (role says 'artifact' —
    # lineage must not care).
    evidence.record(run, "fetcher", ["fetcher/payload.bin"], loc, "complete",
                    role="artifact", workspace=ws)
    # convert: consumed the fetched payload -> derived.
    evidence.record(run, "converter", ["converter/part1.txt"], [], "complete",
                    inputs=["fetcher/payload.bin"], role="artifact", workspace=ws)
    # refine: consumed the derivative -> still derived (chain of two).
    evidence.record(run, "refiner", ["refiner/final.txt"], [], "complete",
                    inputs=["converter/part1.txt"], workspace=ws)
    # search: accessed something else -> proxy.
    evidence.record(run, "searcher", ["searcher/results.json"],
                    ["search-index://x/?q=y"], "partial", workspace=ws)
    # writer: no consumed files, no refs (agent-argument content) -> retained.
    entries = evidence.ledger(ws, (), loc)
    tiers = {e.file: e.tier for e in entries}
    assert tiers["fetcher/payload.bin"] == "target"
    assert tiers["converter/part1.txt"] == "derived"
    assert tiers["refiner/final.txt"] == "derived"
    assert tiers["searcher/results.json"] == "proxy"
    assert tiers["writer/deck.html"] == "retained"      # no provenance -> unknown
    lineage = {e.file for e in evidence.target_lineage(entries)}
    assert lineage == {"fetcher/payload.bin", "converter/part1.txt",
                       "refiner/final.txt"}
    # Content hashes give every payload a stable identity.
    by_file = {e.file: e for e in entries}
    assert by_file["fetcher/payload.bin"].sha256
    # A proxy-derived transformation stays proxy: consuming testimony cannot
    # mint target evidence.
    (ws / "summarizer").mkdir()
    (ws / "summarizer" / "summary.txt").write_bytes(b"s")
    evidence.record(run, "summarizer", ["summarizer/summary.txt"], [], "complete",
                    inputs=["searcher/results.json"], workspace=ws)
    tiers2 = {e.file: e.tier for e in evidence.ledger(ws, (), loc)}
    assert tiers2["summarizer/summary.txt"] == "proxy"


def test_lineage_out_of_order_records_reach_fixpoint(tmp_path):
    """Propagation is a fixpoint, not a single pass: a transformation recorded
    BEFORE its input's fetch still ends up derived."""
    ws = _ws(tmp_path, "a/x.txt", "b/y.txt")
    run = ws.parent
    loc = ["https://example.test/v/abc"]
    evidence.record(run, "b", ["b/y.txt"], [], "complete", inputs=["a/x.txt"],
                    workspace=ws)                        # consumer recorded first
    evidence.record(run, "a", ["a/x.txt"], loc, "complete", workspace=ws)
    tiers = {e.file: e.tier for e in evidence.ledger(ws, (), loc)}
    assert tiers["a/x.txt"] == "target" and tiers["b/y.txt"] == "derived"


# --- the claim-support gate ------------------------------------------------------ #
def _lineage_entries(*files):
    return [evidence.EvidenceEntry(file=f, capability="c", tier="derived",
                                   source_refs=[], coverage="") for f in files]


def test_claims_gate_rules():
    lineage = _lineage_entries("obs/frames/f1.png", "obs/payload.bin")
    ok = [{"claim": "c1", "support": "direct", "evidence": "obs/payload.bin"},
          {"claim": "c2", "support": "inferred", "evidence": "between f1 and f2"}]
    assert _claims_gate(ok, lineage) == ""
    # Missing table -> hollow certificate.
    assert "without a claim-support table" in _claims_gate([], lineage)
    # Unsupported claim -> void.
    bad = ok + [{"claim": "c3", "support": "unsupported", "evidence": ""}]
    assert "unsupported load-bearing claim" in _claims_gate(bad, lineage)
    # All inference/proxy -> void (nothing certifies the source itself).
    soft = [{"claim": "c", "support": "proxy", "evidence": "searcher/results.json"},
            {"claim": "d", "support": "inferred", "evidence": "reasoning"}]
    assert "no claim is supported by target-lineage" in _claims_gate(soft, lineage)
    # A direct claim citing a file OUTSIDE target lineage -> miscited.
    mis = [{"claim": "c", "support": "direct", "evidence": "searcher/results.json"}]
    assert "miscited support" in _claims_gate(mis, lineage)
    # Basename citation resolves (reviewers often cite the short name).
    base = [{"claim": "c", "support": "direct", "evidence": "f1.png at 30s"}]
    assert _claims_gate(base, lineage) == ""


def test_parse_claims_coerces_unknown_support_to_unsupported():
    table = json.dumps([{"claim": "x", "support": "vibes", "evidence": "e"}])
    parsed = _parse_claims("VERDICT: approved\n```claims\n" + table + "\n```")
    assert parsed[0]["support"] == "unsupported"        # fail closed, never open
    assert _parse_claims("no table here") == []


def test_proxy_only_ledger_voids_even_when_nonempty(tmp_path, monkeypatch):
    """The exact hollow-certificate hole: a NON-EMPTY but proxy-only ledger can
    no longer approve claims about the designated source."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer_events = []

    class Tracer(NullTracer):
        def event(self, kind, subject, status="info", **data):
            tracer_events.append((kind, data))

    async def runner(_t, name, prompt, options):
        if name == "gather":
            d = tmp_path / "runs" / "px" / "workspace" / "searcher"
            d.mkdir(parents=True, exist_ok=True)
            (d / "results.json").write_text("{}")
            evidence.record(tmp_path / "runs" / "px", "searcher",
                            ["searcher/results.json"], ["search-index://x/?q=y"],
                            "partial")
            return "searched"
        if name.startswith("review.r"):
            return ("VERDICT: approved\n```claims\n"
                    + json.dumps([{"claim": "c", "support": "proxy",
                                   "evidence": "searcher/results.json"}]) + "\n```")
        return "x"

    bb = Blackboard(tmp_path / "b.db", "px")
    orch = Orchestrator(session="px", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None,
                        planner=StubPlanner([StageSpec(id="gather", pattern="single",
                                                       role="G", goal="summarize")]),
                        resolver=None, session_runner=runner, tracer=Tracer())
    try:
        asyncio.run(orch.run("Explain what happens in https://example.test/v/abc"))
    finally:
        bb.close()
    assert orch.classification == "unverified"
    voids = [d for k, d in tracer_events if k == "review.approval.voided"]
    assert voids and "no target-lineage evidence" in voids[0]["reason"]
    dims = next(d for k, d in tracer_events if k == "run.dimensions")
    assert dims["target_evidence"] == 0


def test_transformation_chain_supports_approval(tmp_path, monkeypatch):
    """The positive case the old gate could not certify: an answer grounded in
    a DERIVED representation approves, with the certificate showing target
    evidence and typed claims."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer_events = []

    class Tracer(NullTracer):
        def event(self, kind, subject, status="info", **data):
            tracer_events.append((kind, data))

    async def runner(_t, name, prompt, options):
        if name == "acquire":
            run = tmp_path / "runs" / "tc"
            ws = run / "workspace"
            for rel in ("fetcher/payload.bin", "converter/sample1.png"):
                p = ws / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"x")
            evidence.record(run, "fetcher", ["fetcher/payload.bin"],
                            ["https://example.test/v/abc"], "complete", workspace=ws)
            evidence.record(run, "converter", ["converter/sample1.png"], [],
                            "complete", inputs=["fetcher/payload.bin"],
                            role="artifact", workspace=ws)
            return "acquired and converted"
        if name.startswith("review.r"):
            return ("VERDICT: approved\n```claims\n"
                    + json.dumps([
                        {"claim": "what the source shows",
                         "support": "derived", "evidence": "converter/sample1.png"},
                        {"claim": "a transition between samples",
                         "support": "inferred", "evidence": "between sample frames"},
                    ]) + "\n```")
        return "the account"

    bb = Blackboard(tmp_path / "b.db", "tc")
    orch = Orchestrator(session="tc", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None,
                        planner=StubPlanner([
                            StageSpec(id="acquire", pattern="single", role="A",
                                      goal="acquire and convert the source"),
                            StageSpec(id="write", pattern="single", role="W",
                                      goal="write the account", depends_on=["acquire"])]),
                        resolver=None, session_runner=runner, tracer=Tracer())
    try:
        asyncio.run(orch.run("Explain what happens in https://example.test/v/abc"))
    finally:
        bb.close()
    assert orch.classification == "succeeded"
    dims = next(d for k, d in tracer_events if k == "run.dimensions")
    assert dims["target_evidence"] == 2                  # payload + derivative
    # The verdict record carries the typed claim table.
    rec = (tmp_path / "runs" / "tc" / "outputs" / "review.md").read_text()
    assert "[derived] what the source shows" in rec
    assert "[inferred] a transition between samples" in rec


def test_miscited_direct_claim_voids(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")

    async def runner(_t, name, prompt, options):
        if name == "gather":
            run = tmp_path / "runs" / "mc"
            ws = run / "workspace"
            p = ws / "fetcher" / "payload.bin"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
            evidence.record(run, "fetcher", ["fetcher/payload.bin"],
                            ["https://example.test/v/abc"], "complete", workspace=ws)
            return "fetched"
        if name.startswith("review.r"):
            return ("VERDICT: approved\n```claims\n"
                    + json.dumps([{"claim": "c", "support": "direct",
                                   "evidence": "notes-from-memory.md"}]) + "\n```")
        return "x"

    bb = Blackboard(tmp_path / "b.db", "mc")
    orch = Orchestrator(session="mc", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None,
                        planner=StubPlanner([StageSpec(id="gather", pattern="single",
                                                       role="G", goal="summarize")]),
                        resolver=None, session_runner=runner)
    try:
        asyncio.run(orch.run("Explain https://example.test/v/abc"))
    finally:
        bb.close()
    assert not orch._review_approved
    assert orch.classification == "unverified"


# --- observation states ----------------------------------------------------------- #
def test_observation_state_contradictions_are_rejected():
    from brain.models import Capability

    def cap():
        return Capability(name="o", description="d", image_tag="i", command=["x"],
                          input_schema={"type": "object", "properties": {}},
                          env_map={}, network="none", emits="observation")

    def check(payload):
        return acquisition._evidence_contract_check(cap(), payload, object())

    ok, d = check({"coverage": "none", "unresolved_needs": ["p"]})
    assert not ok and "OBSERVATION STATE" in d           # state now required
    ok, d = check({"coverage": "none", "unresolved_needs": ["p"],
                   "observation_state": "observed_present"})
    assert not ok and "CONTRADICTORY STATE" in d         # present with zero yield
    ok, d = check({"coverage": "complete", "unresolved_needs": [],
                   "observation_state": "observation_failed"})
    assert not ok and "CONTRADICTORY STATE" in d         # failed but complete
    ok, d = check({"coverage": "none", "unresolved_needs": ["p"],
                   "observation_state": "not_detected"})
    assert ok, d                                          # honest low-sensitivity none
    # The four checks above pin the ENFORCED separation of absence from
    # failure. The prompt wording that teaches it is pinned in exactly one
    # place — tests/test_recovery.py::test_developer_contract_sections_have_
    # not_been_deleted — so a reword fails one test in one file, not three in
    # three, and the failure names the right thing.


def test_ledger_records_observation_state(tmp_path):
    ws = _ws(tmp_path, "obs/t.json")
    evidence.record(ws.parent, "obs", ["obs/t.json"], ["https://example.test/v/a"],
                    "none", state="not_detected", role="observation", workspace=ws)
    e = evidence.ledger(ws, (), ["https://example.test/v/a"])[0]
    assert e.state == "not_detected" and e.role == "observation"
    assert "state=not_detected" in evidence.describe([e], [])


# --- research: enforced bounds + authoritative reading -------------------------- #
def test_research_enforces_its_turn_bound_and_reads_pages(tmp_path, monkeypatch):
    """RESEARCH_AGENT_MAX_TURNS was declared but unused (a 19-turn live session
    proved it); it is now enforced, and the research agent can fetch/read
    authoritative pages, not only search snippets."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    seen = {}

    async def runner(_t, name, prompt, options):
        seen["max_turns"] = getattr(options, "max_turns", None)
        return "findings"

    bb = Blackboard(tmp_path / "b.db", "res")
    orch = Orchestrator(session="res", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=None, resolver=None,
                        session_runner=runner, tracer=NullTracer())
    orch._run_ctx = ("obj", None, tmp_path / "ws")
    caps_asked = {}

    async def fake_acquire_caps(spec):
        caps_asked["caps"] = list(spec.capabilities)
        return {}
    monkeypatch.setattr(orch, "_acquire_caps", fake_acquire_caps)
    try:
        out = asyncio.run(orch._research("an unfamiliar library api"))
    finally:
        bb.close()
    assert seen["max_turns"] == config.RESEARCH_AGENT_MAX_TURNS
    assert caps_asked["caps"] == ["web_search", "fetch_page_text"]
    # The authored fetcher gets a clean instance-neutral contract.
    assert "fetch_page_text" in orch._cap_needs
    assert "URL" in orch._cap_needs["fetch_page_text"]["contract"]
    assert "verify any snippet-only claim" in out


# --- persistence: separate budget, strong-tier judgment -------------------------- #
def test_persistence_budget_is_separate_from_consultations():
    async def decide(moment, digest):
        return ({"action": "keep_trying", "reason": "r"} if moment == "persistence"
                else {"action": "proceed", "reason": "r"})

    sup = Supervisor(decide_fn=decide, max_calls=2)
    # Exhaust the ROUTINE consultation budget…
    asyncio.run(sup.consult("stage_completed", "d"))
    asyncio.run(sup.consult("stage_completed", "d"))
    assert sup.calls == 2
    # …the persistence judgment still runs on its own budget.
    dec = asyncio.run(sup.persistence("digest"))
    assert dec.action == "keep_trying"
    assert sup.persist_calls == 1 and sup.calls == 2
    # And persistence has its own ceiling.
    for _ in range(config.PERSISTENCE_MAX_CALLS + 2):
        asyncio.run(sup.persistence("digest"))
    assert sup.persist_calls == config.PERSISTENCE_MAX_CALLS


# --- expectations persist into the traced plan ----------------------------------- #
def test_stage_expectation_is_persisted_in_the_plan_roster(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")

    async def runner(_t, name, prompt, options):
        return "out"

    bb = Blackboard(tmp_path / "b.db", "exp2")
    orch = Orchestrator(session="exp2", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None,
                        planner=StubPlanner([StageSpec(
                            id="obs", pattern="single", role="O", goal="observe",
                            expectation="full coverage of the requested property")]),
                        resolver=None, session_runner=runner)
    try:
        asyncio.run(orch.run("do the thing"))
        roster = bb.get("plan")
    finally:
        bb.close()
    assert roster[0]["expectation"] == "full coverage of the requested property"
