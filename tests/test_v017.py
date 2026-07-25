"""v0.17 — the review-convergence + measurement-truth release (from campaign
c20260714-1736, cycle 3): the deterministic section counter counted the
document's H1 title as a section, so a fully compliant deliverable (title +
exactly N sections) measured N+1 VIOLATED and reviewers — contractually bound
to trust the engine measurement over their own eyes — burned every bounded
round chasing a phantom; count criteria were measured against a one-line
pointer payload while the graded deliverable was the HTML artifact it pointed
at; round-1 revisions fixed exactly what was critiqued and prepended process
narration the next round was spent on; exhausted reviews sat on precise,
actionable critiques that could never be applied; a healthy revision run
blocked because the planner's revision judgment (a single unchecked LLM
emission) silently failed while the plan itself NAMED the prior-run path it
expected; a cached chart tool's fixed output path made consecutive calls
overwrite each other; and locate_evidence returned false NOT-FOUNDs for
verbatim quotes the reviewer then confirmed by direct file read."""
from __future__ import annotations

import asyncio
import json

from brain import config, criteria, evidence, workflow
from brain.memory.blackboard import Blackboard
from brain.models import Capability, StageSpec
from brain.observability import NullTracer
from brain.orchestrator import (Orchestrator, _claims_gate,
                                _FIXABLE_GATE_PREFIXES, _locate_evidence)
from brain.planner import StubPlanner


class RecordingTracer(NullTracer):
    def __init__(self):
        self.events: list[tuple] = []

    def event(self, kind, subject, status="info", **data):
        self.events.append((kind, subject, status, data))

    def kinds(self):
        return [k for k, *_ in self.events]


def _orch(tmp_path, runner, planner, session, tracer=None, supervisor=None):
    bb = Blackboard(tmp_path / f"{session}.db", session)
    orch = Orchestrator(session=session, manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=planner, resolver=None,
                        session_runner=runner, tracer=tracer, supervisor=supervisor)
    return orch, bb


def _bare_orch(tracer=None):
    o = object.__new__(Orchestrator)
    o.tracer = tracer or NullTracer()
    o._criteria = []
    o._stage_notes = {}
    o._presubmit_fixes_left = 1
    o._revision_fixups_left = 1
    o._gate_fixups_left = 1
    return o


# --- the counter: a document title is not a section -------------------------------- #
def test_title_plus_n_sections_measures_n():
    """The campaign's most consequential finding: title + exactly 5 sections
    measured 6 VIOLATED, and two compliant, factually strong runs shipped
    unverified on rounds spent chasing the phantom."""
    doc = "# Quarterly Review\n\nintro\n\n" + "\n\n".join(
        f"## Part {i}\n\ntext" for i in range(1, 6))
    checks = criteria.check_counts(["exactly 5 sections"], doc)
    assert checks and checks[0].measured == 5 and checks[0].ok


def test_sections_without_a_title_count_at_top_level():
    doc = "# A\n\nx\n\n# B\n\ny\n\n# C\n\nz"
    assert criteria.check_counts(["exactly 3 sections"], doc)[0].measured == 3
    assert criteria.check_counts(["exactly 1 section"], "## Only\n\nbody")[0].measured == 1


def test_subheadings_are_structure_inside_a_section_not_more_sections():
    doc = ("# Title\n\n## First\n\n### depth\n\n### more\n\n## Second\n\n### sub")
    assert criteria.check_counts(["exactly 2 sections"], doc)[0].measured == 2


def test_prepended_narration_heading_is_a_real_violation():
    """The reviser defect the re-measurement loop exists for: a revision
    prepended an '# Extraction Summary' process block — that is a genuine
    structural violation and must still measure as one."""
    doc = ("# Extraction Summary\n\nhow I worked\n\n# The Report\n\n"
           + "\n\n".join(f"## S{i}\n\nt" for i in range(1, 6)))
    check = criteria.check_counts(["exactly 5 sections"], doc)[0]
    # The MEASUREMENT, not just the verdict: `not ok` alone also passes if the
    # counter regressed to counting every heading (7).
    assert check.measured == 2 and not check.ok


def test_heading_unit_still_counts_every_heading():
    doc = "# T\n\n## a\n\n## b"
    assert criteria.check_counts(["at most 3 headings"], doc)[0].measured == 3


def test_describe_states_the_counting_rule():
    checks = criteria.check_counts(["exactly 2 sections"], "# T\n\n## a\n\n## b")
    text = criteria.describe(checks)
    assert "Counting rule" in text and "title" in text


def test_html_projection_counts_sections_bullets_and_ignores_script():
    html = ("<html><head><style>body{color:red}</style></head><body>"
            "<h1>Dash</h1>" +
            "".join(f"<section><h2>S{i}</h2><p>t</p></section>" for i in range(4)) +
            "<ul><li>a</li><li>b</li></ul>"
            "<script>var x = 'not visible words';</script></body></html>")
    text = criteria.html_countable_text(html)
    assert criteria.check_counts(["exactly 4 sections"], text)[0].ok
    assert criteria.check_counts(["at least 2 bullets"], text)[0].ok
    assert "not visible words" not in text


# --- the measurement target follows the deliverable of record ---------------------- #
def _artifact_run(tmp_path, session, name, content, role="artifact"):
    run = tmp_path / "runs" / session
    ws = run / "workspace"
    (ws / "make_report").mkdir(parents=True, exist_ok=True)
    (ws / f"make_report/{name}").write_text(content)
    evidence.record(run, "make_report", [f"make_report/{name}"], [], "complete",
                    role=role, workspace=ws)
    return ws


def test_count_criteria_follow_the_artifact_of_record(tmp_path, monkeypatch):
    """The t4 swing (10/11 -> 6/11 on unchanged analysis quality): the payload
    was a one-line pointer at the HTML artifact, and the artifact — the thing
    the audience gets — was never the thing measured."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    html = ("<h1>Fleet Health</h1>" +
            "".join(f"<section><h2>S{i}</h2><p>data</p></section>" for i in range(4)))
    ws = _artifact_run(tmp_path, "art1", "report.html", html)
    orch = _bare_orch()
    orch._criteria = ["exactly 4 sections"]
    checks, target = orch._measured_checks("see make_report/report.html", ws)
    assert "ARTIFACT" in target and "report.html" in target
    assert checks[0].measured == 4 and checks[0].ok


def test_payload_is_measured_when_no_artifact_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    run = tmp_path / "runs" / "plain1"
    (run / "workspace").mkdir(parents=True)
    orch = _bare_orch()
    orch._criteria = ["exactly 2 sections"]
    checks, target = orch._measured_checks("## a\n\nx\n\n## b\n\ny",
                                           run / "workspace")
    assert target == "the deliverable PAYLOAD"
    assert checks[0].ok


def test_fetched_html_evidence_does_not_hijack_measurement(tmp_path, monkeypatch):
    """Only DECLARED artifact-role output is the deliverable of record — a
    fetched page.html is evidence, not the thing being judged."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    ws = _artifact_run(tmp_path, "obs1", "page.html",
                       "<h2>a</h2><h2>b</h2><h2>c</h2>", role="observation")
    orch = _bare_orch()
    orch._criteria = ["exactly 2 sections"]
    checks, target = orch._measured_checks("## a\n\nx\n\n## b\n\ny", ws)
    assert target == "the deliverable PAYLOAD" and checks[0].ok


# --- pre-review dry-run: the author fixes measured violations ----------------------- #
def test_presubmit_fixup_repairs_a_measured_violation(tmp_path):
    tracer = RecordingTracer()
    orch = _bare_orch(tracer)
    orch._criteria = ["exactly 2 sections"]
    calls = []

    async def redo(feedback, prior=""):
        calls.append(feedback)
        return "## a\n\nx\n\n## b\n\ny"

    ws = tmp_path / "ws"
    ws.mkdir()
    out = asyncio.run(orch._presubmit_format_pass(
        "draft", "## only one\n\nbody", redo, ws))
    assert out.startswith("## a")
    assert orch._presubmit_fixes_left == 0
    assert "criteria.presubmit_violation" in tracer.kinds()
    assert "stage.format_fixup" in tracer.kinds()
    assert "ENGINE PRE-REVIEW MEASUREMENT" in calls[0]


def test_presubmit_keeps_the_original_when_the_fix_measures_worse(tmp_path):
    tracer = RecordingTracer()
    orch = _bare_orch(tracer)
    orch._criteria = ["exactly 2 sections", "at most 40 words"]

    async def redo(feedback, prior=""):
        return "## a\n\n" + "word " * 200   # fixes nothing, adds a violation

    ws = tmp_path / "ws"
    ws.mkdir()
    original = "## one\n\nshort"
    out = asyncio.run(orch._presubmit_format_pass("draft", original, redo, ws))
    assert out == original


def test_presubmit_respects_its_budget(tmp_path):
    orch = _bare_orch()
    orch._criteria = ["exactly 2 sections"]
    orch._presubmit_fixes_left = 0

    async def redo(feedback, prior=""):
        raise AssertionError("must not be called with no budget")

    ws = tmp_path / "ws"
    ws.mkdir()
    out = asyncio.run(orch._presubmit_format_pass("draft", "## one", redo, ws))
    assert out == "## one"


# --- reviser discipline: a revision is re-measured ---------------------------------- #
def test_revision_remeasured_and_corrected(tmp_path):
    """Round-1 revisions fixed the critiqued items and introduced NEW
    structural blocks; round 2 was spent on defects the engine could measure
    the moment the revision landed."""
    tracer = RecordingTracer()
    orch = _bare_orch(tracer)
    orch._criteria = ["exactly 2 sections"]
    outputs = {"draft": "old"}

    async def redo(msg, prior=""):
        assert "ENGINE RE-MEASUREMENT" in msg
        return "## a\n\nx\n\n## b\n\ny"

    ws = tmp_path / "ws"
    ws.mkdir()
    # The t2-c3 shape: the revision fixed the critique and APPENDED a
    # "Sources consulted" block — one section too many, measurable instantly.
    bad_revision = "## a\n\nx\n\n## b\n\ny\n\n## Sources consulted\n\nlinks"
    fixed = asyncio.run(orch._remeasure_revision("draft", bad_revision, redo,
                                                 outputs, ws))
    assert fixed.startswith("## a") and outputs["draft"] == fixed
    assert "review.revision_remeasured" in tracer.kinds()
    assert "review.revision_fixup" in tracer.kinds()


def test_clean_revision_is_not_touched(tmp_path):
    orch = _bare_orch()
    orch._criteria = ["exactly 2 sections"]

    async def redo(msg, prior=""):
        raise AssertionError("clean revision must not trigger a fixup")

    ws = tmp_path / "ws"
    ws.mkdir()
    clean = "## a\n\nx\n\n## b\n\ny"
    assert asyncio.run(orch._remeasure_revision("draft", clean, redo, {}, ws)) == clean


# --- post-review redo: an exhausted critique is applied, once ------------------------ #
def test_exhausted_review_applies_critique_and_buys_one_gated_round(tmp_path,
                                                                    monkeypatch):
    """Three redo_unavailable events in one cycle: reviews exhausted holding
    precise, actionable critiques. The redo applies the final critique and the
    extra round is FULLY gated — approval on it is real approval."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()
    rounds_seen = []

    async def runner(_t, name, prompt, options):
        if name.startswith("check.r"):
            rounds_seen.append(name)
            if len([n for n in rounds_seen if ".r" in n]) == 1:
                return "VERDICT: revise\n1. tighten the intro"
            return "VERDICT: approved\nfixed"
        return "```deliverable\nthe work\n```"

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write"),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], rounds=1)])
    orch, bb = _orch(tmp_path, runner, planner, "postredo1", tracer)
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    assert "review.post_redo" in tracer.kinds()
    assert "review.revised" in tracer.kinds()
    assert orch._review_approved
    assert len(rounds_seen) == 2


def test_post_redo_is_bounded_to_one(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    tracer = RecordingTracer()

    async def runner(_t, name, prompt, options):
        if name.startswith("check.r"):
            return "VERDICT: revise\nstill not right"
        return "```deliverable\nthe work\n```"

    planner = StubPlanner(
        [StageSpec(id="draft", pattern="single", role="D", goal="write"),
         StageSpec(id="check", pattern="review", role="C", goal="verify",
                   depends_on=["draft"], rounds=1)])
    orch, bb = _orch(tmp_path, runner, planner, "postredo2", tracer)
    try:
        asyncio.run(orch.run("write the thing"))
    finally:
        bb.close()
    assert tracer.kinds().count("review.post_redo") == 1
    assert "review.exhausted" in tracer.kinds()
    assert not orch._review_approved


# --- claims-gate dry-run: table mechanics get one re-emission ------------------------ #
class _Entry:
    def __init__(self, file):
        self.file = file


def test_miscited_claim_is_corrected_by_reemission(tmp_path):
    tracer = RecordingTracer()
    orch = _bare_orch(tracer)
    lineage = [_Entry("frames/f001.png"), _Entry("frames/f002.png")]
    claims = [{"claim": "the toad waters the plant", "support": "direct",
               "evidence": "scene notes by the describe stage"}]
    void = _claims_gate(claims, lineage)
    assert void.startswith("miscited support")
    assert void.startswith(_FIXABLE_GATE_PREFIXES)

    corrected = json.dumps([{"claim": "the toad waters the plant",
                             "support": "direct",
                             "evidence": "frames/f001.png"}])

    async def fake_session(spec, caps, objective, upstream, history, workspace,
                           task_context="", **kw):
        assert "CLAIMS-GATE DRY-RUN" in task_context
        assert "frames/f001.png" in task_context
        return "```claims\n" + corrected + "\n```"

    orch._run_agent_with_reflection = fake_session
    stage = StageSpec(id="check", pattern="review", role="C", goal="verify")
    new_void, new_claims = asyncio.run(orch._claims_gate_fixup(
        stage, 1, void, claims, "VERDICT: approved", lineage, "obj", tmp_path))
    assert new_void == ""
    assert new_claims[0]["evidence"] == "frames/f001.png"
    assert any(k == "review.claims_fixup" and d.get("resolved")
               for k, _s, _st, d in tracer.events)


def test_failed_reemission_keeps_the_original_void(tmp_path):
    tracer = RecordingTracer()
    orch = _bare_orch(tracer)
    lineage = [_Entry("frames/f001.png")]
    claims = [{"claim": "c", "support": "direct", "evidence": "nowhere.txt"}]
    void = _claims_gate(claims, lineage)

    async def fake_session(*a, **kw):
        return "I re-affirm my verdict."   # no table re-emitted

    orch._run_agent_with_reflection = fake_session
    stage = StageSpec(id="check", pattern="review", role="C", goal="verify")
    new_void, new_claims = asyncio.run(orch._claims_gate_fixup(
        stage, 1, void, claims, "critique", lineage, "obj", tmp_path))
    assert new_void == void and new_claims == claims


def test_substantive_gate_defects_are_never_fixable():
    """An unsupported claim or a no-grounded-claim table must void, not invite
    a retype under pressure — that would be a laundering channel."""
    lineage = [_Entry("frames/f001.png")]
    unsupported = _claims_gate([{"claim": "c", "support": "unsupported",
                                 "evidence": ""}], lineage)
    proxy_only = _claims_gate([{"claim": "c", "support": "proxy",
                                "evidence": "notes.md"}], lineage)
    assert unsupported and not unsupported.startswith(_FIXABLE_GATE_PREFIXES)
    assert proxy_only and not proxy_only.startswith(_FIXABLE_GATE_PREFIXES)


# --- deterministic revision fallback ------------------------------------------------- #
def test_prior_path_reference_seeds_the_deliverable_without_plan_revision(
        tmp_path, monkeypatch):
    """t5-c3: the planner's stage goal NAMED the prior-run path it expected
    the FAQ at, plan.revision silently never fired, and the run blocked on an
    absence the engine had every signal to prevent at plan time."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    prior = tmp_path / "runs" / "prev-run"
    (prior / "outputs").mkdir(parents=True)
    (prior / "workspace").mkdir()
    (prior / "outputs" / "DELIVERABLE.md").write_text("### Q: one\nanswer\n")
    tracer = RecordingTracer()

    async def runner(_t, name, prompt, options):
        return "revised"

    planner = StubPlanner([StageSpec(
        id="revise", pattern="single", role="E",
        goal="update the FAQ at prior/prev-run/outputs/DELIVERABLE.md")])
    orch, bb = _orch(tmp_path, runner, planner, "fallback1", tracer)
    try:
        asyncio.run(orch.run("update the answer", seed_run="prev-run"))
    finally:
        bb.close()
    ws = tmp_path / "runs" / "fallback1" / "workspace"
    assert (ws / "prior" / "prev-run" / "outputs" / "DELIVERABLE.md").is_file()
    assert "revision.fallback_detected" in tracer.kinds()
    assert "revision.deliverable_seeded" in tracer.kinds()
    entries = evidence.ledger(ws, run_dir=tmp_path / "runs" / "fallback1")
    mine = [e for e in entries if e.file.endswith("outputs/DELIVERABLE.md")]
    assert mine and mine[0].tier == "target"


def test_no_prior_reference_and_no_revision_means_no_seeding(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    prior = tmp_path / "runs" / "prev-run"
    (prior / "outputs").mkdir(parents=True)
    (prior / "workspace").mkdir()
    (prior / "outputs" / "DELIVERABLE.md").write_text("old work")

    async def runner(_t, name, prompt, options):
        return "fresh"

    planner = StubPlanner([StageSpec(id="s", pattern="single", role="W",
                                     goal="a brand new analysis")])
    orch, bb = _orch(tmp_path, runner, planner, "fallback2", RecordingTracer())
    try:
        asyncio.run(orch.run("a new objective", seed_run="prev-run"))
    finally:
        bb.close()
    ws = tmp_path / "runs" / "fallback2" / "workspace"
    assert not (ws / "prior" / "prev-run" / "outputs" / "DELIVERABLE.md").exists()


# --- runtime output collisions -------------------------------------------------------- #
def test_output_collision_lands_at_a_deduped_name(tmp_path):
    from brain.capabilities.runtime import _save_outputs
    first = _save_outputs(tmp_path, "chart_tool", {"chart.png": b"AAA"})
    second = _save_outputs(tmp_path, "chart_tool", {"chart.png": b"BBB"})
    third = _save_outputs(tmp_path, "chart_tool", {"chart.png": b"AAA"})
    assert first == ["chart_tool/chart.png"]
    assert second == ["chart_tool/chart-2.png"]
    assert (tmp_path / "chart_tool/chart.png").read_bytes() == b"AAA"
    assert (tmp_path / "chart_tool/chart-2.png").read_bytes() == b"BBB"
    # Identical bytes are an idempotent rewrite, not a new artifact.
    assert third == ["chart_tool/chart.png"]


# --- cached-tool interface conformance ------------------------------------------------ #
def _cap(schema):
    return Capability(name="writer", description="writes text files",
                      image_tag="t", command=["python"], input_schema=schema,
                      env_map={})


def test_alien_schema_reuse_is_recreated():
    """The t1-c1 shape: a cached writer whose schema demanded its birth task's
    bespoke record fields was reused for plain text-and-filename writes."""
    orch = _bare_orch()
    orch._cap_needs = {"write_text_file": {
        "example_input": {"text": "hello", "filename": "out.md"}}}
    alien = _cap({"properties": {"iso_clause": {}, "evidence_items": {}}})
    reason = orch._contract_mismatch("write_text_file", alien)
    assert "different contract" in reason and "filename" in reason
    fitting = _cap({"properties": {"text": {}, "filename": {}}})
    assert orch._contract_mismatch("write_text_file", fitting) == ""


def test_conformance_passes_without_example_or_schema():
    orch = _bare_orch()
    orch._cap_needs = {}
    assert orch._contract_mismatch("anything", _cap({"properties": {"x": {}}})) == ""
    orch._cap_needs = {"n": {"example_input": {"a": 1}}}
    assert orch._contract_mismatch("n", _cap({})) == ""
    # `files` is engine-mounted, never a schema obligation.
    orch._cap_needs = {"n": {"example_input": {"files": ["input/a.pdf"]}}}
    assert orch._contract_mismatch("n", _cap({"properties": {"pages": {}}})) == ""


# --- markup integrity at verification -------------------------------------------------- #
class _Res:
    def __init__(self, files):
        self.output_files = files


def test_truncated_html_fails_verification():
    from brain.acquisition import _markup_integrity_check
    ok, detail = _markup_integrity_check(_Res(
        {"report.html": b"<html><body><section><h2>S1</h2><p>text</p><div class="}))
    assert not ok and "MARKUP INTEGRITY" in detail


def test_structurally_unbalanced_html_fails_verification():
    from brain.acquisition import _markup_integrity_check
    body = b"<html><body>" + b"<section><h2>s</h2>" * 4 + b"</body></html>"
    ok, detail = _markup_integrity_check(_Res({"report.html": body}))
    assert not ok and "section" in detail


def test_complete_html_and_non_markup_outputs_pass():
    from brain.acquisition import _markup_integrity_check
    good = (b"<html><body>"
            + b"<section><h2>s</h2><p>t</p></section>" * 4
            + b"</body></html>")
    assert _markup_integrity_check(_Res({"report.html": good}))[0]
    assert _markup_integrity_check(_Res({"data.json": b'{"a": "<div"}'}))[0]
    assert _markup_integrity_check(_Res({}))[0]


# --- locate_evidence recall ------------------------------------------------------------ #
def _evidence_ws(tmp_path, text, name="doc.txt", binary=None):
    ws = tmp_path / "ws"
    (ws / "read_doc").mkdir(parents=True, exist_ok=True)
    if binary is not None:
        (ws / "read_doc" / name).write_bytes(binary)
    else:
        (ws / "read_doc" / name).write_text(text)
    return ws


def test_lookup_normalizes_typography_and_markdown(tmp_path):
    ws = _evidence_ws(tmp_path,
                      "The firm’s ARR reached **€7.9M** — “a record year”.")
    assert _locate_evidence(ws, ("read_doc",),
                            "the firm's ARR reached €7.9M").startswith("FOUND")
    assert _locate_evidence(ws, ("read_doc",),
                            '"a record year"').startswith("FOUND")


def test_lookup_survives_a_bad_byte_in_the_file(tmp_path):
    """errors='strict' skipped the WHOLE file over one undecodable byte — a
    verbatim quote in retained evidence came back NOT FOUND and the reviewer
    had to re-verify by direct read."""
    payload = "prefix \xff\xfe noise\n".encode("latin-1") + \
        "the patent grant survives termination".encode()
    ws = _evidence_ws(tmp_path, "", binary=payload)
    assert _locate_evidence(ws, ("read_doc",),
                            "patent grant survives termination").startswith("FOUND")


def test_lookup_reports_ordered_token_matches_as_approximate(tmp_path):
    ws = _evidence_ws(tmp_path,
                      "Revenue grew steadily, reaching €7.896M by the period's end.")
    out = _locate_evidence(ws, ("read_doc",), "revenue reaching €7.896M period")
    assert out.startswith("FOUND") and "APPROXIMATE" in out
    missing = _locate_evidence(ws, ("read_doc",), "quarterly safety incidents")
    assert missing.startswith("NOT FOUND")
