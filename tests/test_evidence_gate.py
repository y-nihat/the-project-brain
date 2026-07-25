"""The deterministic trust core (V060-YouTube review): the evidence ledger,
the approval gate it powers, honest courier labeling, escalation of unmet
evidence demands into the adaptive loop, the typed deliverable payload, and
engine-measured count criteria. The failure these pin: a reviewer acknowledged
an empty evidence plane, then approved on courier narration with all 17
deterministic lookups NOT FOUND — and the engine took the parsed verdict over
its own facts, shipping SUCCEEDED with zero retained evidence and a
six-block 'exactly one paragraph' deliverable."""
from __future__ import annotations

import asyncio
import json

from brain import acquisition, config, criteria, evidence, workflow
from brain.memory.blackboard import Blackboard
from brain.models import StageSpec
from brain.observability import NullTracer
from brain.orchestrator import Orchestrator
from brain.planner import StubPlanner
from brain.registry import Registry
from brain.sandbox.manager import RunResult


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


def _retain(tmp_path, session, cap, name, refs, content=b"retained payload"):
    """Simulate what the capability runtime does for a real retention: write
    the payload AND record its provenance — stubs must honor the real contract
    (a stub that skips the ledger is how a broken certificate ships green)."""
    d = tmp_path / "runs" / session / "workspace" / cap
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(content)
    evidence.record(tmp_path / "runs" / session, cap, [f"{cap}/{name}"], refs, "complete")
    return f"{cap}/{name}"


def _approved(evidence_file):
    """A protocol-complete approval: verdict + typed claim-support table citing
    the target-lineage file (the gate voids approvals without one)."""
    return ("VERDICT: approved\n```claims\n"
            + json.dumps([{"claim": "the deliverable's load-bearing claim",
                           "support": "direct", "evidence": evidence_file}])
            + "\n```")


# --- the ledger ----------------------------------------------------------------- #
def test_ledger_tiers_retained_evidence(tmp_path):
    ws = tmp_path / "run" / "workspace"
    (ws / "input").mkdir(parents=True)
    (ws / "input" / "doc.pdf").write_bytes(b"%PDF")
    (ws / "fetch_page").mkdir()
    (ws / "fetch_page" / "page.html").write_text("<html>target</html>")
    (ws / "web_search").mkdir()
    (ws / "web_search" / "results.json").write_text("{}")
    (ws / "legacy_tool").mkdir()
    (ws / "legacy_tool" / "old.txt").write_text("no provenance")
    evidence.record(ws.parent, "fetch_page", ["fetch_page/page.html"],
                    ["https://example.test/v/abc"], "complete")
    evidence.record(ws.parent, "web_search", ["web_search/results.json"],
                    ["search-index://duckduckgo/?q=abc"], "partial")

    locators = evidence.designated_locators(
        "Explain what happens in https://example.test/v/abc please")
    assert locators == ["https://example.test/v/abc"]
    entries = evidence.ledger(ws, ("fetch_page", "web_search", "legacy_tool"), locators)
    tiers = {e.file: e.tier for e in entries}
    assert tiers["input/doc.pdf"] == "target"          # user-designated by construction
    assert tiers["fetch_page/page.html"] == "target"   # accessed the designated locator
    assert tiers["web_search/results.json"] == "proxy"  # accessed the index, not the target
    assert tiers["legacy_tool/old.txt"] == "retained"  # unknown lineage, still visible
    text = evidence.describe(entries, locators)
    assert "[target] fetch_page/page.html" in text and "[proxy]" in text
    # Empty ledger states the fact that matters.
    assert "EMPTY" in evidence.describe([], locators)
    # No target-lineage evidence -> the limitation is stated.
    only_proxy = [e for e in entries if e.tier == "proxy"]
    assert "no target-lineage evidence" in evidence.describe(only_proxy, locators)
# --- the approval gate ----------------------------------------------------------- #
def test_approved_over_empty_ledger_is_voided(tmp_path, monkeypatch):
    """A parsed 'approved' cannot override the engine's facts: source-derived
    run + zero retained evidence => approval voided, courier dispatched, and an
    unresolved demand ends UNVERIFIED with the gap escalated to the loop."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    prompts: dict[str, str] = {}

    async def runner(_t, name, prompt, options):
        prompts[name] = prompt
        if name.startswith("review.r"):
            return "VERDICT: approved\nAll good."     # narration-backed approval
        return f"out({name})"

    planner = StubPlanner([StageSpec(id="gather", pattern="single", role="G",
                                     goal="summarize the source")])
    orch, bb = _orch(tmp_path, runner, planner, "void1", tracer)
    try:
        answer = asyncio.run(orch.run(
            "Explain what happens in https://example.test/v/abc"))
        gaps = bb.get("evidence.gaps") or []
    finally:
        bb.close()
    assert orch.classification == "unverified"
    assert "UNVERIFIED" in answer
    assert "approved-voided" in orch._review_verdicts
    assert not orch._review_approved
    assert "review.approval.voided" in tracer.kinds()
    # The courier ran, retained nothing, and was labeled as narration.
    assert "courier.ineffective" in tracer.kinds()
    fix_prompt = prompts.get("gather.fix", "")
    assert "narration, NOT evidence" in fix_prompt
    assert "NEWLY RETAINED EVIDENCE" not in fix_prompt
    # The unmet demand became controller state for the adaptive loop.
    assert gaps and "retained observation" in gaps[0]["need"]
    # The authoritative record shows the void.
    rec = (tmp_path / "runs" / "void1" / "outputs" / "review.md").read_text()
    assert "approved-voided" in rec


def test_approval_stands_with_retained_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")

    async def runner(_t, name, prompt, options):
        if name == "gather":
            _retain(tmp_path, "ok1", "observer", "payload.txt",
                    ["https://example.test/v/abc"])
            return "observed"
        if name.startswith("review.r"):
            return _approved("observer/payload.txt")
        return "x"

    planner = StubPlanner([StageSpec(id="gather", pattern="single", role="G",
                                     goal="summarize the source")])
    orch, bb = _orch(tmp_path, runner, planner, "ok1")
    try:
        asyncio.run(orch.run("Explain what happens in https://example.test/v/abc"))
    finally:
        bb.close()
    assert orch.classification == "succeeded"
    assert orch._review_approved


def test_effective_courier_is_labeled_by_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    prompts: dict[str, str] = {}
    verdicts = iter(["VERDICT: needs-evidence\n1. fetch and retain the source page.",
                     _approved("fetcher/page.html")])

    async def runner(_t, name, prompt, options):
        prompts[name] = prompt
        if name == "review.evidence":
            _retain(tmp_path, "cour1", "fetcher", "page.html",
                    ["https://example.test/v/abc"], b"<html>the page</html>")
            return "fetched and RETAINED the page"
        if name.startswith("review.r"):
            return next(verdicts)
        return f"out({name})"

    planner = StubPlanner([StageSpec(id="gather", pattern="single", role="G",
                                     goal="summarize the source")])
    orch, bb = _orch(tmp_path, runner, planner, "cour1")
    try:
        asyncio.run(orch.run("Explain what happens in https://example.test/v/abc"))
    finally:
        bb.close()
    assert "NEWLY RETAINED EVIDENCE" in prompts["gather.fix"]
    assert "fetcher/page.html" in prompts["gather.fix"]
    assert orch.classification == "succeeded"          # round 2 approved over real files


def test_unmet_evidence_demand_escalates_to_extension_and_recheck(tmp_path, monkeypatch):
    """The courier's structural limit (it can only re-run the tools whose
    insufficiency caused the demand) no longer dead-ends the run: the demand
    becomes an evidence gap, the planner extends with a retention-capable
    stage, a FRESH review verifies over the new evidence, and only then does
    the run read SUCCEEDED."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    ran: list[str] = []

    class ExtendingPlanner(StubPlanner):
        async def extend(self, objective, completed, gaps, existing_ids):
            assert any("retained observation" in g["need"] for g in gaps)
            return ([StageSpec(id="observe_direct", pattern="single", role="O",
                               goal="observe the designated source and retain payloads")],
                    [], {})

    async def runner(_t, name, prompt, options):
        ran.append(name)
        if name == "observe_direct":
            _retain(tmp_path, "esc1", "direct_observer", "payload.bin",
                    ["https://example.test/v/abc"], b"observed bytes")
            return "direct observation retained"
        if name.startswith("review-recheck"):
            return _approved("direct_observer/payload.bin")
        if name.startswith("review."):
            return "VERDICT: needs-evidence\n1. retain observation of the source."
        return f"out({name})"

    planner = ExtendingPlanner([StageSpec(id="summ", pattern="single", role="S",
                                          goal="summarize the source")])
    orch, bb = _orch(tmp_path, runner, planner, "esc1", tracer)
    try:
        answer = asyncio.run(orch.run(
            "Explain what happens in https://example.test/v/abc"))
    finally:
        bb.close()
    assert "review.reappended" in tracer.kinds()
    assert any(n.startswith("review-recheck.r") for n in ran)
    assert ran.index("observe_direct") < ran.index("review-recheck.r1")
    assert orch.classification == "succeeded"
    assert "direct observation retained" in answer     # recheck's draft is the new terminal


# --- typed deliverable payload + measured criteria -------------------------------- #
def test_deliverable_payload_extraction():
    """Extraction only. The FILE half of the contract — that the payload
    reaches outputs/DELIVERABLE.md — is covered by
    test_measured_violation_voids_approval_until_fixed, which reads it."""
    text = ("Here is my work.\n```deliverable\nThe exact paragraph.\n```\n"
            "Notes: I counted the sentences.")
    assert workflow.extract_deliverable(text) == "The exact paragraph."
    assert workflow.extract_deliverable("no fence here") is None
    two = "```deliverable\nfirst\n```\nx\n```deliverable\nsecond\n```"
    assert workflow.extract_deliverable(two) == "second"


def test_deliverable_stage_ids_cover_review_draft_and_terminal():
    stages = [StageSpec(id="a", pattern="single", role="r", goal="g"),
              StageSpec(id="b", pattern="single", role="r", goal="g", depends_on=["a"]),
              StageSpec(id="rev", pattern="review", role="r", goal="g", depends_on=["b"])]
    assert workflow.deliverable_stage_ids(stages) == {"b"}
    assert workflow.deliverable_stage_ids(stages[:2]) == {"b"}


def test_count_criteria_measured_and_gating():
    checks = criteria.check_counts(
        ["The deliverable is exactly one paragraph.",
         "The paragraph contains no more than ten sentences.",
         "under 40 words"],
        "Para one.\n\nPara two is here. It has more. Sentences three.")
    by_unit = {c.unit: c for c in checks}
    assert by_unit["paragraph"].measured == 2 and not by_unit["paragraph"].ok
    assert by_unit["paragraph"].gating
    assert by_unit["sentence"].ok and not by_unit["sentence"].gating   # advisory
    assert by_unit["word"].ok and by_unit["word"].gating
    v = criteria.violations(checks)
    assert [c.unit for c in v] == ["paragraph"]
    assert "VIOLATED" in criteria.describe(checks)
    # Word-number and 'a single X' forms parse; unparseable criteria are skipped.
    assert criteria.check_counts(["a single paragraph"], "one\n\ntwo")[0].ok is False
    assert criteria.check_counts(["be clear and honest"], "x") == []


def test_measured_violation_voids_approval_until_fixed(tmp_path, monkeypatch):
    """The V060 shape: the reviewer counts only the embedded paragraph and
    approves; the engine measures the PAYLOAD, voids, and the fix round ships
    a fenced single paragraph — which becomes DELIVERABLE.md verbatim."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    bad = "Preamble first.\n\nThe actual paragraph, alone.\n\nChange log: fixed things."
    good = ("Notes before.\n```deliverable\nThe actual paragraph, alone.\n```\n"
            "Sentence count: 1.")

    async def runner(_t, name, prompt, options):
        if name == "write":
            return bad
        if name == "write.fix":
            assert "ENGINE GATE" in prompt and "measured 3" in prompt
            return good
        if name.startswith("check.r"):
            return "VERDICT: approved"
        raise AssertionError(name)

    planner = StubPlanner(
        [StageSpec(id="write", pattern="single", role="W", goal="write it"),
         StageSpec(id="check", pattern="review", role="C", goal="check it",
                   depends_on=["write"], rounds=2)],
        success_criteria=["The deliverable is exactly one paragraph."])
    orch, bb = _orch(tmp_path, runner, planner, "fmt1", tracer)
    try:
        asyncio.run(orch.run("write me exactly one paragraph about anything"))
    finally:
        bb.close()
    assert "review.approval.voided" in tracer.kinds()
    assert orch.classification == "succeeded"          # fixed, re-approved honestly
    pure = (tmp_path / "runs" / "fmt1" / "outputs" / "DELIVERABLE.md").read_text()
    assert pure == "The actual paragraph, alone."


def test_lingering_measured_violation_classifies_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")

    async def runner(_t, name, prompt, options):
        return "One.\n\nTwo.\n\nThree."                # violates everywhere, never fixed

    planner = StubPlanner([StageSpec(id="w", pattern="single", role="W", goal="g")],
                          success_criteria=["exactly one paragraph"])
    orch, bb = _orch(tmp_path, runner, planner, "fmt2")
    try:
        answer = asyncio.run(orch.run("write one paragraph"))
    finally:
        bb.close()
    assert orch.classification == "partial"
    assert "criteria_format_violation" in answer
    assert "measured 3" in answer


def test_unparsed_review_verdict_does_not_auto_approve(tmp_path, monkeypatch):
    """A review whose prose lacks the exact VERDICT token is a protocol
    violation — it must default to 'revise', never 'approved' (observed: a
    synthesis grounded in ZERO observed frames was auto-approved on an
    unparsed verdict)."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")

    async def runner(_t, name, prompt, options):
        if name == "gather":
            d = tmp_path / "runs" / "unp" / "workspace" / "obs"
            d.mkdir(parents=True, exist_ok=True)
            (d / "p.txt").write_text("payload")
            return "observed"
        if name.startswith("review.r"):
            return "This looks basically fine to me — nice work overall."  # no VERDICT line
        return "x"

    planner = StubPlanner([StageSpec(id="gather", pattern="single", role="G",
                                     goal="summarize the source")])
    orch, bb = _orch(tmp_path, runner, planner, "unp")
    try:
        asyncio.run(orch.run("Explain what happens in https://example.test/v/abc"))
    finally:
        bb.close()
    assert not orch._review_approved
    assert orch.classification == "unverified"
    assert "revise" in orch._review_verdicts


def test_per_criterion_verdicts_parse_into_the_record(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    table = json.dumps([{"criterion": "one paragraph", "status": "met",
                         "evidence": "engine measurement"},
                        {"criterion": "claims traceable", "status": "unknown",
                         "evidence": "no retained evidence"}])

    async def runner(_t, name, prompt, options):
        if name == "gather":
            d = tmp_path / "runs" / "crit1" / "workspace" / "obs"
            d.mkdir(parents=True, exist_ok=True)
            (d / "p.txt").write_text("payload")
            return "observed"
        if name.startswith("review.r"):
            return "VERDICT: approved\n```criteria\n" + table + "\n```"
        return "x"

    planner = StubPlanner([StageSpec(id="gather", pattern="single", role="G",
                                     goal="summarize the source")])
    orch, bb = _orch(tmp_path, runner, planner, "crit1")
    try:
        asyncio.run(orch.run("Explain https://example.test/v/abc"))
    finally:
        bb.close()
    rec = (tmp_path / "runs" / "crit1" / "outputs" / "review.md").read_text()
    assert "[met] one paragraph — engine measurement" in rec
    assert "[unknown] claims traceable" in rec


# --- one evidence contract for every observer (recipe path) ----------------------- #
def _res(stdout, files=None, exit_code=0):
    return RunResult(container_id="c", exit_code=exit_code, stdout=stdout, stderr="",
                     timed_out=False, output_files=files or {})


class ScriptedManager:
    def __init__(self, results):
        self._results = list(results)
        self.built: list[str] = []

    def build_image(self, tag, ctx):
        self.built.append(tag)

    def run_ephemeral(self, *a, **kw):
        return self._results.pop(0) if self._results else self._results[0]


def test_recipe_web_search_holds_the_full_evidence_contract(tmp_path):
    """The bootstrap path gets no privileged bypass: a recipe result without
    yield/lineage fields fails generic verification; the compliant recipe
    passes both the generic contract and the shape verifier, and is stamped
    with the current validation epoch."""
    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    bad = json.dumps({"query": "q", "results": [
        {"title": "t", "href": "h", "body": "b"}]})    # no coverage/refs/retention
    good = json.dumps({"query": "q",
                       "results": [{"title": "t", "href": "h", "body": "b"}],
                       "source_refs": ["search-index://duckduckgo/?q=q"],
                       "coverage": "partial",
                       "observation_state": "observed_present",
                       "unresolved_needs": ["full content of the result pages"],
                       "consumed_inputs": []})
    mgr = ScriptedManager([
        _res(bad),                                                  # a1 generic -> fail
        _res(good, files={"search-results.json": b"{}"}),           # a2 generic -> ok
        # The recipe path now runs the SAME fabrication battery as authored
        # tools and as revalidation ("recipes get no privileged bypass"): the
        # no-input probe applies (the recipe declares env-mapped arguments) and
        # the shipped program answers honestly, exactly as WEB_SEARCH_PROGRAM
        # does — {"error": "no query provided"}, exit 2. The irrelevant-input
        # probe is exempt here (the recipe consumes no `files`), so it costs no
        # container run.
        _res(json.dumps({"error": "no query provided"}), exit_code=2),  # a2 no-input probe
        _res(good, files={"search-results.json": b"{}"}),           # a2 shape  -> ok
    ])
    cap = asyncio.run(acquisition.acquire(
        "web_search", manager=mgr, registry=reg, broker=None, builder=None,
        tracer=NullTracer()))
    stamped = reg.get("web_search").validated_engine
    reg.close()
    assert cap.verified
    assert stamped == acquisition.validation_epoch()
    # The shipped recipe program itself complies (retention + lineage + yield).
    p = acquisition.WEB_SEARCH_PROGRAM
    assert "search-results-" in p                       # unique per call: no overwrite
    assert "observation_state" in p
    assert "source_refs" in p and "coverage" in p and "unresolved_needs" in p
    # The substring checks above are a PROXY: WEB_SEARCH_PROGRAM only executes
    # under Docker, so the offline suite has no other way to tie the SHIPPED
    # script to the retention/lineage/yield contract. Its live equivalent,
    # test_m1_web_search.py::test_acquire_verify_promote_web_search, is skipped
    # without a daemon. The recipe's internal arity is deliberately NOT pinned:
    # the acquire() call above unpacks the same tuple and drives both
    # verifiers, so a wrong arity fails there with a real error.


# --- perception: image observations are presentable -------------------------------- #
def test_image_blocks_bounded_and_typed(tmp_path):
    from brain.capabilities.runtime import image_blocks
    ws = tmp_path / "ws"
    (ws / "cap").mkdir(parents=True)
    (ws / "cap" / "frame1.png").write_bytes(b"\x89PNG fake bytes")
    (ws / "cap" / "frame2.jpg").write_bytes(b"\xff\xd8 fake")
    (ws / "cap" / "notes.txt").write_text("not an image")
    (ws / "cap" / "huge.png").write_bytes(b"x" * 4096)
    blocks = image_blocks(["cap/frame1.png", "cap/notes.txt", "cap/frame2.jpg",
                           "cap/huge.png"], ws, max_images=2, max_bytes=1024)
    # Non-images are skipped and the count cap holds.
    assert [b["mimeType"] for b in blocks] == ["image/png", "image/jpeg"]
    assert all(b["type"] == "image" and b["data"] for b in blocks)

    # The CURRENT oversize contract, which had inverted since this was written:
    # image_blocks now FITS an over-budget image and drops it only when it
    # cannot be fitted ("FIT, then drop — never the reverse"). The old comment
    # claimed the oversize path was covered while the count cap stopped the
    # loop before huge.png was ever examined.
    fitted = image_blocks(["cap/huge.png"], ws, max_images=4, max_bytes=1024)
    assert len(fitted) <= 1
    for b in fitted:
        assert len(b["data"]) <= 1024 * 2      # base64 of a fitted payload


# --- the strategy space must include the target's own signals ---------------------- #
def test_the_planner_prompt_keeps_its_epistemic_sections_and_no_trial_residue():
    """A PROMPT-CONTENT PROXY for behaviour that needs a live LLM, plus the
    architectural-law guard. Named for what it is: eight verbatim phrases
    (including the spacing of 'direct observation > ') broke on any reword
    while proving nothing about planning.

    The negative loop below is the load-bearing half and is kept verbatim —
    it is the only mechanical guard that no diagnostic trial's subject leaks
    into what every future plan is designed against.
    """
    from brain import planner as planner_mod
    s = planner_mod._SYSTEM
    low = s.lower()
    for section in ("TARGET SIGNAL MODEL", "BELIEF HYGIENE"):
        assert section in s, f"section {section!r} deleted"
    for concept in ("direct observation", "derived representation",
                    "never a blocker", "circular"):
        assert concept in low, f"the prompt lost the {concept!r} rule"
    # No TASK-specific residue from the diagnostic trials. (Generic multimodal
    # vocabulary — vlm/ocr/caption/frame — is allowed: it teaches a general
    # rule that applies to charts, scans, screenshots, any image task.)
    for residue in ("youtube", "youtu", "transcript", ".mp4", "ffmpeg",
                    "smolvlm", "video", "audio track"):
        assert residue not in s.lower(), f"trial residue {residue!r} in planner prompt"
