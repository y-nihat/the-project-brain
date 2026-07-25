"""Epistemic-architecture hardening from the MVP review (issues #1-#8), pure parts:
fabrication probe, provenance headers, objective success criteria, observed-artifact
inspection. Docker/LLM behavior is covered by the existing suites + live runs.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from brain import acquisition, config
from brain.models import Capability, StageSpec
from brain.orchestrator import _observed_artifacts, artifact_issues
from brain.planner import StubPlanner


# --- #1/#6 fabrication probe ---------------------------------------------------- #
class _FakeManager:
    """Runs are simulated; records whether it was called with input files."""
    def __init__(self, stdout_no_input='{"results": ["fabricated!"]}'):
        self.stdout_no_input = stdout_no_input
        self.calls: list[dict] = []

    def run_ephemeral(self, image, command, *, policy, capability, environment=None,
                      input_files=None, capture_output_files=False, **kw):
        self.calls.append({"input_files": input_files})
        # No input -> whatever the tool would fabricate; kept simple/deterministic.
        return SimpleNamespace(ok=True, oom=False, exit_code=0, timed_out=False,
                               stdout=self.stdout_no_input, stderr="", output_files={},
                               error_note=None)


def _cap(**kw):
    base = dict(name="read_pdf", description="d", image_tag="i:1", command=["x"],
                input_schema={"type": "object", "properties": {"files": {"type": "array"}}},
                env_map={})
    base.update(kw)
    return Capability(**base)


def test_probe_rejects_fabricator_that_declares_inputs():
    cap = _cap()                         # declares a `files` input
    # A read_pdf-style fabricator: substantive invented content from no input.
    fake_doc = json.dumps({"content": "Constitutional AI and RLHF ensure models are "
                                      "helpful, harmless, and honest. " * 20})
    mgr = _FakeManager(fake_doc)
    ok, detail = acquisition._no_input_probe(cap, {"sample_files": {"x.pdf": "y"}}, mgr, None)
    assert not ok and "FABRICATION PROBE FAILED" in detail


def test_probe_passes_empty_degenerate_result():
    # A tool returning an empty/degenerate result without input is honest, not
    # fabricating (e.g. an echo/combine tool) — must not be flagged.
    cap = _cap(input_schema={"type": "object", "properties": {"files": {"type": "array"}}})
    mgr = _FakeManager('{"combined": ""}')
    ok, _ = acquisition._no_input_probe(cap, {"sample_files": {"a": "b"}}, mgr, None)
    assert ok


def test_probe_flags_output_files_from_no_input():
    cap = _cap()
    mgr = _FakeManager('{"ok": true}')
    mgr.run_ephemeral = lambda *a, **k: SimpleNamespace(  # produced a file from nothing
        ok=True, oom=False, exit_code=0, timed_out=False, stdout='{"ok": true}',
        stderr="", output_files={"sample.pdf": b"%PDF fabricated"}, error_note=None)
    ok, detail = acquisition._no_input_probe(cap, {"sample_files": {"x": "y"}}, mgr, None)
    assert not ok and "FABRICATION" in detail


def test_probe_passes_tool_that_errors_without_input():
    cap = _cap()
    mgr = _FakeManager('{"error": "no PDF supplied"}')   # fails legibly
    ok, detail = acquisition._no_input_probe(cap, {"sample_files": {"x.pdf": "y"}}, mgr, None)
    assert ok


def test_probe_exempts_pure_generators():
    # A tool declaring NO inputs (generator) is allowed to produce from nothing.
    cap = _cap(input_schema={"type": "object", "properties": {}}, env_map={})
    mgr = _FakeManager('{"results": ["generated"]}')
    ok, _ = acquisition._no_input_probe(cap, {}, mgr, None)
    assert ok
    assert mgr.calls == []               # exempt tools aren't even probed


def test_declares_inputs_detection():
    assert acquisition._declares_inputs(_cap(), {})                       # files in schema
    assert acquisition._declares_inputs(_cap(env_map={"q": "QUERY"}), {}) # env-mapped arg
    assert acquisition._declares_inputs(
        _cap(input_schema={"type": "object", "properties": {}}), {"sample_files": {"a": "b"}})
    assert not acquisition._declares_inputs(
        _cap(input_schema={"type": "object", "properties": {}}, env_map={}), {})


# --- #11 applicability + #10 observation-based provenance ------------------------ #
def test_incompatible_refs_blocks_wrong_types():
    from brain.capabilities.runtime import incompatible_refs
    assert incompatible_refs(["input/x.md", "input/y.pdf"], [".pdf"]) == ["input/x.md"]
    assert incompatible_refs(["a.PDF"], ["pdf"]) == []          # case/dot tolerant
    assert incompatible_refs(["a.md"], []) == []                # no declaration => no gate
    assert incompatible_refs(None, [".pdf"]) == []


def test_provenance_reports_actual_consumption():
    from brain.capabilities.runtime import provenance_line
    # Consumed vs routed-but-ignored (the sample_claude.pdf laundering case).
    line = provenance_line("read_pdf", ["read_pdf/report.md"],
                           {"consumed_inputs": [], "generated": True})
    assert "consumed NO input files" in line and "NOT derived from user evidence" in line
    assert "NON-EVIDENCE" in line
    line2 = provenance_line("read_pdf", ["input/a.pdf", "notes.md"],
                            {"consumed_inputs": ["a.pdf"]})
    assert "consumed input(s): ['a.pdf']" in line2
    assert "NOT consumed" in line2 and "notes.md" in line2
    # Legacy tool that reports nothing -> derivation explicitly unverified.
    line3 = provenance_line("old_tool", ["input/a.pdf"], {"data": 1})
    assert "UNVERIFIED" in line3


def test_verify_requires_consumed_inputs_for_file_tools():
    mgr = _CapturingManager('{"combined": "AB"}')               # no consumed_inputs
    cap = _cap(name="combiner")
    ok, detail, _ = acquisition._generic_verify(cap, {}, {"a.txt": "A"}, mgr, None)
    assert not ok and "consumed_inputs" in detail
    mgr2 = _CapturingManager('{"combined": "AB", "consumed_inputs": ["a.txt"]}')
    ok2, _, _ = acquisition._generic_verify(cap, {}, {"a.txt": "A"}, mgr2, None)
    assert ok2


class _CapturingManager:
    def __init__(self, stdout):
        self.stdout = stdout

    def run_ephemeral(self, image, command, *, policy, capability, environment=None,
                      input_files=None, capture_output_files=False, **kw):
        return SimpleNamespace(ok=True, oom=False, exit_code=0, timed_out=False,
                               stdout=self.stdout, stderr="", output_files={},
                               error_note=None)


# --- #9 immune system: revalidation + quarantine --------------------------------- #
def test_revalidation_quarantines_legacy_fabricator(tmp_path):
    from brain.registry import Registry
    from brain.resolver import Resolver

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    # emits declared => passes the contract-staleness gate, so the PROBE decides.
    fabricator = _cap(name="legacy_reader", verified=True, emits="observation")
    reg.add(fabricator)
    fake_doc = json.dumps({"content": "substantive invented analysis " * 30})
    mgr = _FakeManager(fake_doc)
    mgr.ensure_image = lambda tag: None
    mgr.build_image = lambda *a, **k: None

    ok, reason = acquisition.revalidate_capability(reg.get("legacy_reader"), mgr, reg)
    assert not ok and "PROBE FAILED" in reason
    reg.quarantine("legacy_reader", reason)
    # Quarantined memory is never reused: the resolver forces re-authoring.
    assert Resolver(reg).resolve("legacy_reader").kind == "create"
    reg.close()


def test_revalidation_stamps_honest_tool(tmp_path):
    from brain.registry import Registry

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    honest = _cap(name="honest_reader", verified=True, emits="observation")
    reg.add(honest)
    mgr = _FakeManager('{"error": "no input supplied"}')        # fails legibly
    mgr.ensure_image = lambda tag: None

    ok, reason = acquisition.revalidate_capability(reg.get("honest_reader"), mgr, reg)
    assert ok
    reg.stamp_validated("honest_reader", config.ENGINE_VERSION)
    got = reg.get("honest_reader")
    assert got.validated_engine == config.ENGINE_VERSION and not got.quarantined
    reg.close()


def test_revalidation_rejects_pre_contract_reader(tmp_path):
    """A file-consuming capability with NO declared output role predates the
    typed-evidence contract: it must be re-authored, not re-stamped — the
    observed failure was a biased reader staying 'validated' across a contract
    change because only the broad engine label was compared."""
    from brain.registry import Registry

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    stale = _cap(name="old_reader", verified=True, accepts=[".pdf"])   # emits: undeclared
    reg.add(stale)
    ok, reason = acquisition.revalidate_capability(reg.get("old_reader"), None, reg)
    assert not ok and "older capability contract" in reason
    reg.close()


def test_validation_epoch_fingerprints_the_contract(monkeypatch):
    """The validation stamp must move when the authoring contract changes, even
    if ENGINE_VERSION does not — otherwise new rules never reach old tools."""
    e1 = acquisition.validation_epoch()
    assert e1.startswith(config.ENGINE_VERSION + "+c")
    monkeypatch.setattr(acquisition, "_DEVELOPER_SYSTEM",
                        acquisition._DEVELOPER_SYSTEM + "\nNEW RULE.")
    assert acquisition.validation_epoch() != e1


def test_registry_roundtrips_emits_and_syncs_manifest(tmp_path):
    """DB and the human-facing manifest must agree about trust state — the
    observed run had db=quarantined while manifest.json said null."""
    from brain.registry import Registry

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    reg.add(_cap(name="writer", verified=True, emits="artifact"))
    assert reg.get("writer").emits == "artifact"
    mf = tmp_path / "store" / "writer" / "manifest.json"
    reg.quarantine("writer", "test reason")
    assert json.loads(mf.read_text())["quarantined"] == "test reason"
    reg.stamp_validated("writer", "9.9.9+cabc")
    data = json.loads(mf.read_text())
    assert data["quarantined"] is None and data["validated_engine"] == "9.9.9+cabc"
    reg.close()


# --- #12 regression: the REAL capture path keeps everything emitted -------------- #
def test_plan_capture_keeps_all_emitted_keys():
    # The emit_plan handler's contract: a call with any recognized parameter is
    # captured WHOLE (cherry-picking 'stages' silently dropped success_criteria
    # once) — extra keys ride along untouched.
    from brain.observability import NullTracer
    from brain.planner import _make_emit_plan

    captured: dict = {}
    handler = _make_emit_plan(captured, NullTracer(), "planner")
    args = {"stages": [{"id": "s", "pattern": "single", "role": "r", "goal": "g"}],
            "success_criteria": ["claims traceable to the attached file"],
            "future_key": "kept too"}
    out = asyncio.run(handler(dict(args)))
    assert not out.get("is_error")
    assert captured == args
    # A payload with NO stages at all: the criteria must still survive, which
    # is the property cherry-picking broke. (This replaces a source grep for
    # one exact assignment spelling — blind to every other way of
    # reintroducing the bug, and breakable by an unrelated local named
    # `captured`.)
    captured2: dict = {}
    handler2 = _make_emit_plan(captured2, NullTracer(), "planner")
    out2 = asyncio.run(handler2({"success_criteria": ["under 40 words"]}))
    assert not out2.get("is_error")
    assert captured2["success_criteria"] == ["under 40 words"]


# --- #13 engine-appended grounding review ---------------------------------------- #
def test_user_evidence_forces_a_review_even_in_a_2_stage_plan():
    # The exact ebook shape: read -> present, no review, user file attached.
    from brain import workflow
    from brain.models import StageSpec
    stages = [StageSpec(id="read", pattern="single", role="r", goal="read the file"),
              StageSpec(id="present", pattern="single", role="r", goal="deck", depends_on=["read"])]
    out, appended = workflow.append_review_if_needed(stages, has_user_evidence=True)
    assert appended and any(s.pattern == "review" for s in out)
    assert out[-1].depends_on == ["present"]


def test_no_user_evidence_shallow_chain_gets_no_review():
    from brain import workflow
    from brain.models import StageSpec
    stages = [StageSpec(id="a", pattern="single", role="r", goal="g"),
              StageSpec(id="b", pattern="single", role="r", goal="g", depends_on=["a"])]
    out, appended = workflow.append_review_if_needed(stages, has_user_evidence=False)
    assert not appended and not any(s.pattern == "review" for s in out)


def test_existing_review_is_not_duplicated():
    from brain import workflow
    from brain.models import StageSpec
    stages = [StageSpec(id="a", pattern="single", role="r", goal="g"),
              StageSpec(id="chk", pattern="review", role="c", goal="v", depends_on=["a"])]
    out, appended = workflow.append_review_if_needed(stages, has_user_evidence=True)
    assert not appended and sum(s.pattern == "review" for s in out) == 1


def test_transformation_depth():
    from brain import workflow
    from brain.models import StageSpec
    chain = [StageSpec(id="a", pattern="single", role="r", goal="g"),
             StageSpec(id="b", pattern="single", role="r", goal="g", depends_on=["a"]),
             StageSpec(id="c", pattern="single", role="r", goal="g", depends_on=["b"])]
    assert workflow._transformation_depth(chain) == 3


# --- #16 artifact hygiene -------------------------------------------------------- #
def test_artifact_issues_flags_real_truncation(tmp_path):
    ws = tmp_path / "workspace" / "write_file"
    ws.mkdir(parents=True)
    # The exact reported fragments (definite), a complete title (must NOT flag),
    # and a clean sentence.
    (ws / "deck.html").write_text(
        '<title>Claude — Composable, Safety-First AI</title>'
        '<p>Three pill tags: Trust s</p>'
        '<p>event-driven coordination (reactive trigger). Source: analy</p>'
        '<p>A complete, well-formed sentence.</p>')
    issues = artifact_issues(tmp_path / "workspace")
    text = " ".join(issues)
    assert "Trust s" in text and "Source: analy" in text
    assert "Safety-First AI" not in text           # complete title -> no false positive
    definite = artifact_issues(tmp_path / "workspace", definite_only=True)
    assert definite and all("[definite]" in i for i in definite)


def test_artifact_issues_ignores_css_js_and_flags_markup_leaks(tmp_path):
    """CSS inside <style> is not visible text (the false positive that burned
    repair cycles); raw markdown in RENDERED text and a path-as-title are
    genuine defects (both observed in the delivered v0.3.0 artifact)."""
    ws = tmp_path / "workspace" / "write_file"
    ws.mkdir(parents=True)
    (ws / "deck.html").write_text(
        "<title>input/claude-ebook.pdf (30 pages, ~7,900 words)</title>"
        "<style>.a{animation:x 4s linear infinite;transform-origin:720px 450px}</style>"
        "<script>let x = {a: 1, b: 2}</script>"
        "<p>**COMPOSABLE BY DESIGN** — animated dashed lines</p>"
        "<p>A complete, well-formed sentence.</p>")
    issues = artifact_issues(tmp_path / "workspace")
    text = " ".join(issues)
    assert "transform-origin" not in text          # CSS is not visible text
    assert "raw markdown" in text                  # ** leaked into rendered copy
    assert "file path" in text                     # <title> is a path, not a title


def test_artifact_issues_clean_output(tmp_path):
    ws = tmp_path / "workspace" / "write_file"
    ws.mkdir(parents=True)
    (ws / "deck.html").write_text(
        "<h1>Composable AI</h1><p>A complete, well-formed sentence.</p>")
    assert artifact_issues(tmp_path / "workspace") == []


def test_truncation_confidence_tiers():
    from brain.orchestrator import _truncation_confidence
    # STRUCTURAL evidence of a cut => definite (gates repair/classification).
    assert _truncation_confidence("Three pill tags: Trust s") == "definite"
    assert _truncation_confidence("footer note: Source: analy") == "definite"
    assert _truncation_confidence("Violet/t") == "definite"           # pair cut at the slash
    assert _truncation_confidence("event-driven coordination (reactive trigge") == "definite"
    # Sentence-shaped lowercase ending WITHOUT structural evidence is only a
    # suspicion for review — it can just as well be a complete caption.
    assert _truncation_confidence("agents reason dynamically and handle") == "suspected"
    assert _truncation_confidence("Green palette on dar") == "suspected"
    assert _truncation_confidence("Claude — Composable, Safety-First AI") is None  # complete
    assert _truncation_confidence("A complete sentence here.") is None


def test_ordinary_captions_are_not_mechanical_defects(tmp_path):
    """The v0.3.2 false positive: complete unpunctuated captions were labeled
    'definite' truncation, causing two full rewrites and the quarantine of a
    healthy verbatim writer. Captions must produce ZERO repair-gating findings."""
    from brain.orchestrator import _truncation_confidence
    assert _truncation_confidence("How Claude works") is None
    assert _truncation_confidence("Why it matters") is None
    ws = tmp_path / "workspace" / "write_file"
    ws.mkdir(parents=True)
    (ws / "deck.html").write_text(
        "<title>Composable AI</title>"
        '<div class="credit">How Claude works</div>'
        '<div class="credit">Why it matters</div></body></html>')
    assert artifact_issues(tmp_path / "workspace", definite_only=True) == []


def test_source_evidence_reads_extraction_not_artifact(tmp_path):
    from brain.orchestrator import _source_evidence
    ws = tmp_path / "workspace"
    (ws / "read_pdf").mkdir(parents=True)
    (ws / "read_pdf" / "report.txt").write_text("Title: Building Effective AI Agents. architecture.")
    (ws / "write_file").mkdir(parents=True)
    (ws / "write_file" / "deck.html").write_text("<h1>WHO IS CLAUDE</h1>")
    # Reader dirs come from declared contracts (emits), never hardcoded names.
    ev = _source_evidence(ws, reader_dirs=("read_pdf",))
    assert "Building Effective AI Agents" in ev        # extraction IS source evidence
    assert "WHO IS CLAUDE" not in ev                   # the artifact is NOT
    assert "Building" not in _source_evidence(ws)      # undeclared -> input/ only


def test_source_evidence_never_empty_for_oversize_extraction(tmp_path):
    """The v0.3.2 packet bug: an extraction LARGER than the budget made the
    assembly loop break before adding anything, so review ran with ZERO ground
    truth exactly when the evidence was substantial. A partial first chunk must
    be included, headers counted inside the budget."""
    from brain.orchestrator import _source_evidence
    ws = tmp_path / "workspace"
    (ws / "read_pdf").mkdir(parents=True)
    (ws / "read_pdf" / "extract.txt").write_text(
        "Title: Building Effective AI Agents\n" + ("architecture patterns " * 600))
    ev = _source_evidence(ws, reader_dirs=("read_pdf",), limit=6000)
    assert ev, "oversize evidence must yield a PARTIAL packet, never an empty one"
    assert "Building Effective AI Agents" in ev
    assert len(ev) <= 6200                             # budget-compliant (± the marker)
    assert "evidence continues" in ev                  # the cut is labeled, not silent


def test_reader_dirs_use_declared_output_role(tmp_path):
    """Source evidence comes from capabilities that DECLARE observation output.
    'Consumes files' is not an epistemic role — a validator consumes files too,
    and its verdicts must never enter the ORIGINAL SOURCE EVIDENCE packet."""
    from brain.orchestrator import Orchestrator
    from brain.registry import Registry

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    reg.add(_cap(name="extractor", verified=True, accepts=[".pdf"], emits="observation"))
    reg.add(_cap(name="checker", verified=True, accepts=[".html"], emits="validation"))
    reg.add(_cap(name="writer", verified=True, emits="artifact"))
    dirs = Orchestrator._reader_dirs(SimpleNamespace(registry=reg))
    assert dirs == ("extractor",)
    reg.close()


def test_reader_dirs_legacy_fallback_uses_accepts(tmp_path):
    from brain.orchestrator import Orchestrator
    from brain.registry import Registry

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    legacy = _cap(name="old_reader", verified=True, accepts=[".pdf"])
    legacy.emits = ""                                   # promoted before typing existed
    reg.add(legacy)
    dirs = Orchestrator._reader_dirs(SimpleNamespace(registry=reg))
    assert dirs == ("old_reader",)                      # degrade gracefully, not silently
    reg.close()


def test_reader_dirs_never_empty_when_file_grounded_output_exists(tmp_path):
    """An extractor that under-declares itself 'derived' must not silence the
    whole evidence surface (observed live: locate_evidence searched 0 files and
    review lost its ground truth). With no observation-role capability, file-
    consuming caps still count as the evidence surface — but validators and
    artifact producers never do."""
    from brain.orchestrator import Orchestrator
    from brain.registry import Registry

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    reg.add(_cap(name="modest_extractor", verified=True, accepts=[".pdf"], emits="derived"))
    reg.add(_cap(name="checker", verified=True, accepts=[".html"], emits="validation"))
    reg.add(_cap(name="writer", verified=True, emits="artifact"))
    dirs = Orchestrator._reader_dirs(SimpleNamespace(registry=reg))
    assert dirs == ("modest_extractor",)
    reg.close()


# --- causal attribution: verbatim conduits + detector self-suspicion ------------- #
def test_monitor_verbatim_exoneration_evidence():
    from brain.capabilities.health import HealthMonitor
    m = HealthMonitor()
    m.note_verbatim("write_file", True)
    m.note_verbatim("write_file", True)
    assert m.verbatim["write_file"] is True
    m.note_verbatim("write_file", False)                # one transforming write
    m.note_verbatim("write_file", True)
    assert m.verbatim["write_file"] is False            # ...clears it for good


def test_repair_stops_on_unchanged_defect_signature(tmp_path, monkeypatch):
    """Two identical false findings burned 459s/$1.11 in v0.3.2: an attempt
    that changes nothing means the DETECTOR is the suspect — the same repair
    path must not run again."""
    from brain.observability import NullTracer
    from brain.orchestrator import Orchestrator

    ws = tmp_path / "workspace" / "write_file"
    ws.mkdir(parents=True)
    (ws / "deck.html").write_text("<p>footer note: Source: analy</p>")  # a definite defect
    calls = []

    async def redo(guidance, out):
        calls.append(guidance)          # "repairs" without changing the file
        return out

    o = SimpleNamespace(_redo={"design": redo}, tracer=NullTracer(),
                        _repair_stalled=False)
    out = asyncio.run(Orchestrator._repair_artifacts(
        o, "design", {}, tmp_path / "workspace", "draft"))
    assert len(calls) == 1              # attempt 2 must NOT run on the same signature
    assert o._repair_stalled is True
    assert out == "draft"


def test_repair_embeds_complete_content_or_skips(tmp_path, monkeypatch):
    """A 27KB artifact was silently clipped to a 24k prefix labeled 'CURRENT
    content' — creating the truncation the repair exists to fix. Content is now
    embedded WHOLE, or the file is skipped honestly."""
    from brain.observability import NullTracer
    from brain.orchestrator import Orchestrator

    ws = tmp_path / "workspace" / "write_file"
    ws.mkdir(parents=True)
    body = "<p>real content here</p>" * 1200            # ~30KB, > the old 24k clip
    (ws / "deck.html").write_text(body + "<p>footer note: Source: analy</p>")
    seen = {}

    async def redo(guidance, out):
        seen["guidance"] = guidance
        return out

    o = SimpleNamespace(_redo={"design": redo}, tracer=NullTracer(),
                        _repair_stalled=False)
    asyncio.run(Orchestrator._repair_artifacts(
        o, "design", {}, tmp_path / "workspace", "draft"))
    assert body in seen["guidance"]                     # the WHOLE file, not a prefix
    # And a file beyond the budget is skipped, never clipped-and-mislabeled.
    monkeypatch.setattr(config, "REPAIR_MAX_CONTENT_CHARS", 1000)
    seen.clear()
    o2 = SimpleNamespace(_redo={"design": redo}, tracer=NullTracer(),
                         _repair_stalled=False)
    asyncio.run(Orchestrator._repair_artifacts(
        o2, "design", {}, tmp_path / "workspace", "draft"))
    assert "guidance" not in seen                       # no clipped repair attempt ran


def test_locate_evidence_settles_claims_by_direct_lookup(tmp_path):
    """A reviewer adjudicates 'is this figure in the source?' by lookup, not by
    asking the producing stage: a tool-less follow-up agent testified a verbatim
    source figure ('90.2%') was 'possibly hallucinated', and a TRUE claim was
    removed on that false testimony."""
    from brain.orchestrator import _locate_evidence
    ws = tmp_path / "workspace"
    (ws / "read_doc").mkdir(parents=True)
    (ws / "read_doc" / "extract.txt").write_text(
        "multi-agent systems \noutperform single-agent systems by 90.2%. Also:\n"
        "20 to 60 percent \nproductivity gains were reported.")
    (ws / "write_out").mkdir(parents=True)
    (ws / "write_out" / "deck.html").write_text("<p>93.7% invented figure</p>")

    hit = _locate_evidence(ws, ("read_doc",), "90.2%")
    assert hit.startswith("FOUND") and "extract.txt:2" in hit
    # Matches across the evidence file's line wrapping.
    assert _locate_evidence(ws, ("read_doc",), "20 to 60 percent productivity").startswith("FOUND")
    # An absent figure gets an authoritative NOT FOUND — that IS the adjudication.
    assert _locate_evidence(ws, ("read_doc",), "93.7%").startswith("NOT FOUND")
    # Artifacts are never searched: a draft must not confirm itself.
    assert "deck.html" not in _locate_evidence(ws, ("read_doc",), "93.7%")
    assert "too short" in _locate_evidence(ws, ("read_doc",), "%")


def test_review_record_is_the_aggregate_authority(tmp_path, monkeypatch):
    """outputs/<review>.md must carry the reviewer's verdicts — not the revised
    draft's self-description ('All reviewer defects resolved' while the final
    structured verdict said revise)."""
    from brain.orchestrator import Orchestrator

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    o = SimpleNamespace(session="sess-1")
    Orchestrator._write_review_record(
        o, "review",
        [{"round": 1, "verdict": "revise", "critique": "VERDICT: revise\n1. fix title"},
         # A critique whose PROSE claims success while its structured verdict
         # says otherwise — the exact shape the record must not be fooled by.
         {"round": 2, "verdict": "revise",
          "critique": "All reviewer defects resolved.\nVERDICT: revise\n1. still wrong"}],
        "revise — rounds exhausted; the final round's critique was NOT applied")
    text = (tmp_path / "runs" / "sess-1" / "outputs" / "review.md").read_text()
    assert "verdict record (authoritative)" in text
    assert "FINAL: revise — rounds exhausted" in text
    # The STRUCTURED verdict outranks the prose, which is also present.
    assert "round 2 — VERDICT: revise" in text
    assert "All reviewer defects resolved" in text


# --- #15 general-contract consolidation ------------------------------------------ #
def test_generalize_description_strips_task_residue():
    d = acquisition.generalize_description(
        "Read input/claude-ebook.pdf and analyze it; write to workspace/out/deck.html")
    assert "claude-ebook.pdf" not in d and "input/" not in d and "workspace/out" not in d
    assert "a provided file" in d


# --- #3 success criteria threading ---------------------------------------------- #
# --- #4 observed-artifact inspection -------------------------------------------- #
def test_observed_artifacts_reads_outputs_but_skips_inputs_and_prior(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "write_file").mkdir(parents=True)
    (ws / "write_file" / "deck.html").write_text("<h1>PAGE 1 —</h1>real output")
    (ws / "input").mkdir()
    (ws / "input" / "source.pdf").write_bytes(b"%PDF-1.7 user file")
    (ws / "prior" / "run-x").mkdir(parents=True)
    (ws / "prior" / "run-x" / "old.txt").write_text("seeded from a past run")

    observed = _observed_artifacts(ws)
    assert "deck.html" in observed and "real output" in observed
    assert "source.pdf" not in observed      # user input is not this run's output
    assert "old.txt" not in observed         # prior-run seed excluded


def test_observed_artifacts_empty_workspace(tmp_path):
    assert _observed_artifacts(tmp_path / "nope") == ""
