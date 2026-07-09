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
                      input_files=None, capture_output_files=False):
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
                      input_files=None, capture_output_files=False):
        return SimpleNamespace(ok=True, oom=False, exit_code=0, timed_out=False,
                               stdout=self.stdout, stderr="", output_files={},
                               error_note=None)


# --- #9 immune system: revalidation + quarantine --------------------------------- #
def test_revalidation_quarantines_legacy_fabricator(tmp_path):
    from brain.registry import Registry
    from brain.resolver import Resolver

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    fabricator = _cap(name="legacy_reader", verified=True)      # no validation stamp
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
    honest = _cap(name="honest_reader", verified=True)
    reg.add(honest)
    mgr = _FakeManager('{"error": "no input supplied"}')        # fails legibly
    mgr.ensure_image = lambda tag: None

    ok, reason = acquisition.revalidate_capability(reg.get("honest_reader"), mgr, reg)
    assert ok
    reg.stamp_validated("honest_reader", config.ENGINE_VERSION)
    got = reg.get("honest_reader")
    assert got.validated_engine == config.ENGINE_VERSION and not got.quarantined
    reg.close()


# --- #12 regression: the REAL capture path keeps everything emitted -------------- #
def test_plan_capture_keeps_all_emitted_keys():
    # Mirrors the emit_plan handler's contract: captured.update(args) — the bug
    # was cherry-picking 'stages' and silently dropping success_criteria.
    captured: dict = {}
    captured.update({"stages": [{"id": "s", "pattern": "single", "role": "r", "goal": "g"}],
                     "success_criteria": ["claims traceable to the attached file"]})
    assert captured.get("success_criteria") == ["claims traceable to the attached file"]
    import inspect

    from brain import planner as planner_mod
    src = inspect.getsource(planner_mod)
    assert 'captured["stages"] =' not in src   # the exact bug shape must not return
    assert "captured.update(args" in src


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


def test_artifact_issues_clean_output(tmp_path):
    ws = tmp_path / "workspace" / "write_file"
    ws.mkdir(parents=True)
    (ws / "deck.html").write_text(
        "<h1>Composable AI</h1><p>A complete, well-formed sentence.</p>")
    assert artifact_issues(tmp_path / "workspace") == []


def test_truncation_confidence_tiers():
    from brain.orchestrator import _truncation_confidence
    assert _truncation_confidence("Three pill tags: Trust s") == "definite"
    assert _truncation_confidence("footer note: Source: analy") == "definite"
    assert _truncation_confidence("event-driven coordination (reactive trigge") == "definite"
    assert _truncation_confidence("agents reason dynamically and handle") == "definite"
    assert _truncation_confidence("Claude — Composable, Safety-First AI") is None  # complete
    assert _truncation_confidence("A complete sentence here.") is None


def test_source_evidence_reads_extraction_not_artifact(tmp_path):
    from brain.orchestrator import _source_evidence
    ws = tmp_path / "workspace"
    (ws / "read_pdf").mkdir(parents=True)
    (ws / "read_pdf" / "report.txt").write_text("Title: Building Effective AI Agents. architecture.")
    (ws / "write_file").mkdir(parents=True)
    (ws / "write_file" / "deck.html").write_text("<h1>WHO IS CLAUDE</h1>")
    ev = _source_evidence(ws)
    assert "Building Effective AI Agents" in ev        # extraction IS source evidence
    assert "WHO IS CLAUDE" not in ev                   # the artifact is NOT


# --- #15 general-contract consolidation ------------------------------------------ #
def test_generalize_description_strips_task_residue():
    d = acquisition.generalize_description(
        "Read input/claude-ebook.pdf and analyze it; write to workspace/out/deck.html")
    assert "claude-ebook.pdf" not in d and "input/" not in d and "workspace/out" not in d
    assert "a provided file" in d


# --- #3 success criteria threading ---------------------------------------------- #
def test_plan_carries_success_criteria():
    p = asyncio.run(StubPlanner(
        [StageSpec(id="s", pattern="single", role="r", goal="g")],
        success_criteria=["exactly 3 sections", "under 40 words"],
    ).plan("make a 3-page thing"))
    assert p.success_criteria == ["exactly 3 sections", "under 40 words"]


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
