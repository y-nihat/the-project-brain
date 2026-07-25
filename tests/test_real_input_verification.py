"""Real-input verification + judgment visibility (the archive-input run):
a tool whose genuine input is a real/binary file could never see it at
verification — sample_input.files was silently DISCARDED — so every such tool
failed 'file not found' three times regardless of code quality, and the
persistence judgment (which could not see input/) asked the user to re-upload
a file that was sitting in the workspace all along. Also: REPL multi-line
objectives."""
from __future__ import annotations

import asyncio
import json

from brain import acquisition, config
from brain.memory.blackboard import Blackboard
from brain.models import Capability, MethodOutcome, StageSpec
from brain.observability import NullTracer
from brain.orchestrator import Orchestrator
from brain.registry import Registry
from brain.repl import read_objective
from brain.sandbox.manager import RunResult


def _res(stdout, files=None, exit_code=0):
    return RunResult(container_id="c", exit_code=exit_code, stdout=stdout, stderr="",
                     timed_out=False, output_files=files or {})


class CapturingManager:
    """Records what each sandbox invocation actually mounted (the fabrication
    probe legitimately runs with none, so keep the full call history)."""

    def __init__(self, result):
        self._result = result
        self.calls: list = []

    @property
    def mounted(self):
        return self.calls[-1] if self.calls else None

    def build_image(self, tag, ctx):
        pass

    def run_ephemeral(self, *a, environment=None, input_files=None, **kw):
        self.calls.append(input_files)
        return self._result


def _tool_cap():
    return Capability(name="t", description="d", image_tag="i:1", command=["x"],
                      input_schema={"type": "object",
                                    "properties": {"path": {"type": "string"},
                                                   "files": {"type": "array"}}},
                      env_map={"path": "ARCHIVE_PATH"}, network="none")


# --- verification mounts REAL workspace files ------------------------------------ #
def test_verification_mounts_real_workspace_files(tmp_path):
    ws = tmp_path / "ws"
    (ws / "input").mkdir(parents=True)
    payload = b"PK\x03\x04 real binary bytes \x00\x01\x02"
    (ws / "input" / "provided.bin").write_bytes(payload)

    ok_json = json.dumps({"ok": True, "consumed_inputs": ["provided.bin"]})
    mgr = CapturingManager(_res(ok_json))
    ok, detail, _ = acquisition._generic_verify(
        _tool_cap(), {"path": "input/provided.bin", "files": ["input/provided.bin"]},
        {}, mgr, workspace=ws)
    assert ok, detail
    # Mounted at BASENAME with the REAL bytes — runtime parity.
    assert mgr.mounted == {"provided.bin": payload}


def test_verification_flags_missing_or_unavailable_sample_files(tmp_path):
    ws = tmp_path / "ws"
    (ws / "input").mkdir(parents=True)
    mgr = CapturingManager(_res("{}"))
    # Reference that does not resolve -> instructive failure, not silent drop.
    ok, detail, _ = acquisition._generic_verify(
        _tool_cap(), {"files": ["input/nope.bin"]}, {}, mgr, workspace=ws)
    assert not ok and "SAMPLE FILE NOT FOUND" in detail
    assert mgr.mounted is None                        # never ran the container
    # Escape attempts are refused by the traversal guard.
    ok2, d2, _ = acquisition._generic_verify(
        _tool_cap(), {"files": ["../../etc/passwd"]}, {}, mgr, workspace=ws)
    assert not ok2 and "SAMPLE FILE NOT FOUND" in d2
    # References without an attached workspace -> instructive failure.
    ok3, d3, _ = acquisition._generic_verify(
        _tool_cap(), {"files": ["input/x.bin"]}, {}, mgr, workspace=None)
    assert not ok3 and "SAMPLE FILES UNAVAILABLE" in d3


def test_author_loop_threads_workspace_to_verification(tmp_path):
    """acquire() -> _author_capability -> _generic_verify carries the run
    workspace, so a developer referencing the task's real file verifies
    against it (the failing archive run's exact missing link)."""
    ws = tmp_path / "runs" / "s" / "workspace"
    (ws / "input").mkdir(parents=True)
    (ws / "input" / "data.bin").write_bytes(b"\x00\x01real")
    reg = Registry(tmp_path / "r.db", tmp_path / "store")

    async def dev(need_key, description, previous=None, error=None):
        return {"program": "p", "dockerfile": "FROM python:3.12-slim",
                "command": ["python", "-B", "/app/tool.py"],
                "input_schema": {"type": "object",
                                 "properties": {"path": {"type": "string"},
                                                "files": {"type": "array"}}},
                "env_map": {"path": "P"}, "network": "none",
                "sample_input": {"path": "input/data.bin",
                                 "files": ["input/data.bin"]},
                "emits": "observation"}

    ok_json = json.dumps({"ok": True, "consumed_inputs": ["data.bin"],
                          "coverage": "complete", "unresolved_needs": [],
                          "observation_state": "observed_present"})
    mgr = CapturingManager(_res(ok_json))
    cap = asyncio.run(acquisition.acquire(
        "read_binary_thing", manager=mgr, registry=reg, broker=None, developer=dev,
        description="read a provided binary file", tracer=NullTracer(), workspace=ws))
    reg.close()
    assert cap.verified
    # The VERIFY call mounted the real file; the fabrication battery then ran
    # both probes — one with NO input (its whole point) and one with input the
    # tool cannot have parsed. The no-input probe is no longer last, because
    # promotion now runs the same battery revalidation runs.
    verify_call = mgr.calls[0]
    assert verify_call and verify_call["data.bin"] == b"\x00\x01real"
    assert None in mgr.calls[1:]                       # no-input probe ran
    assert any(c and "__probe__.txt" in c for c in mgr.calls[1:])   # irrelevant-input probe ran


def test_developer_contract_teaches_real_file_verification():
    c = acquisition._DEVELOPER_SYSTEM
    assert "the engine mounts those REAL files" in c
    assert "never fabricate a binary fixture" in c
    assert "SYNTHETIC TEXT fixture" in c


# --- the judgment sees the user's provided files ---------------------------------- #
def test_persistence_digest_and_help_request_see_input_files(tmp_path):
    ws = tmp_path / "runs" / "j" / "workspace"
    (ws / "input").mkdir(parents=True)
    (ws / "input" / "provided.zip").write_bytes(b"PK\x03\x04" + b"x" * 100)
    bb = Blackboard(tmp_path / "b.db", "j")
    orch = Orchestrator(session="j", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=None, resolver=None,
                        tracer=NullTracer())
    orch._run_ctx = ("obj", None, ws)
    stage = StageSpec(id="unpack", pattern="single", role="U", goal="extract the archive")
    outcome = MethodOutcome(stage_id="unpack", requirement="extract", phase="acquisition",
                            failure_class="implementation", detail="file not found",
                            attempts=3)
    orch._failures.append(outcome)
    digest = orch._persistence_digest(stage, outcome)
    assert "USER-PROVIDED INPUT FILES" in digest
    assert "input/provided.zip" in digest
    assert "does NOT need to re-provide" in digest
    orch._help_request = {"requirement": "extract", "why": "w", "needed": "n",
                          "tried": ["unpack"], "retained": [],
                          "inputs": orch._input_files(ws)}
    block = orch._help_request_block()
    bb.close()
    assert "Your provided files (already in the run, no re-upload needed)" in block
    assert "input/provided.zip" in block


# --- REPL multi-line objectives ---------------------------------------------------- #
def test_read_objective_multiline_forms():
    # Plain single line passes through untouched (commands included).
    assert read_objective("/help") == "/help"
    assert read_objective("do the thing") == "do the thing"
    # A bracketed paste arrives as ONE string with embedded newlines — untouched.
    pasted = "line one\nline two\nline three"
    assert read_objective(pasted) == pasted
    # Triple-quote block: reads until the closing marker.
    feed = iter(["second line", "third line", '"""'])
    out = read_objective('"""first line', lambda _p: next(feed))
    assert out == "first line\nsecond line\nthird line"
    # Closing marker on the last content line.
    feed2 = iter(['tail line"""'])
    assert read_objective('"""head', lambda _p: next(feed2)) == "head\ntail line"
    # One-line form.
    assert read_objective('"""all in one"""') == "all in one"
    # EOF mid-block returns what was gathered.
    def eof(_p):
        raise EOFError
    assert read_objective('"""only', eof) == "only"
