"""The outcome loop (v0.27.0): standing goals, outcome verdicts, bounded pursuit.

Post-mortem (three manual follow-ups on one day): the system fetched a real
external outcome itself, inherited that knowledge into the next run — and
still needed the human to be the outcome monitor ('improve it'), the
continuation judge ('research first'), and the goal memory. A run classified
'succeeded' while the measured outcome REGRESSED, because success was judged
against process-shaped criteria, never against the aspiration. These tests
pin the goal layer: aspirations persist as standing goals, outcomes are
judged against them (real measurements only, never self-estimates), a
regression mandates research-first, and self-initiated pursuit runs ONLY
under a user-granted budget with fail-CLOSED judgment.
"""
from __future__ import annotations

import asyncio

from brain import acquisition, config
from brain import goals as goals_mod
from brain.acquisition import AcquisitionError
from brain.memory.goals import GoalLedger, goals_block
from brain.models import Capability
from brain.observability import NullTracer
from brain.planner import _normalize_aspiration
from brain.registry import Registry


# --- the ledger ------------------------------------------------------------------ #
def test_goal_ledger_roundtrip_and_direction_best(tmp_path):
    gl = GoalLedger(tmp_path / "g.db")
    gid = gl.declare("public evaluation score", "maximize", target="0.8",
                     measured_by="the evaluator's API response")
    gl.record_attempt(gid, "run-1", value="0.772", verdict="advanced")
    gl.record_attempt(gid, "run-2", value="0.755", verdict="regressed")
    g = gl.get(gid)
    assert g["status"] == "open" and g["direction"] == "maximize"
    assert gl.best_value(g) == "0.772"                    # direction-aware
    g2 = gl.get(gl.declare("latency", "minimize"))
    gl.record_attempt(g2["id"], "r", value="120", verdict="advanced")
    gl.record_attempt(g2["id"], "r2", value="90", verdict="advanced")
    assert gl.best_value(g2) == "90"                      # minimize picks lowest
    g3 = gl.get(gl.declare("latency", "reach", target="200"))
    for v in ("500", "300", "250"):
        gl.record_attempt(g3["id"], "r", value=v, verdict="advanced")
    assert gl.best_value(g3) == "250"                     # reach = closest to target
    block = goals_block(gl)
    assert "goal #" in block and "public evaluation score" in block
    assert "goal_ref" in block                            # instructs continuation
    gl.mark(gid, "achieved")
    assert all(x["id"] != gid for x in gl.open_goals())   # achieved leaves the list
    gl.close()


# --- aspiration normalization ---------------------------------------------------- #
def test_normalize_aspiration_rejects_garbage_and_keeps_budgets_honest():
    assert _normalize_aspiration(None) == {}
    assert _normalize_aspiration({"direction": "maximize"}) == {}   # no metric, no ref
    a = _normalize_aspiration({"metric": "score", "direction": "sideways",
                               "budget_attempts": "3", "budget_cost_usd": -5})
    assert a["direction"] == "maximize"          # bad direction coerced
    assert a["budget_attempts"] == 3
    assert "budget_cost_usd" not in a            # non-positive budget dropped
    assert _normalize_aspiration({"goal_ref": 7})["goal_ref"] == "7"


# --- outcome judgment: coercion, fail-open, regression rule ---------------------- #
def test_judge_outcome_coerces_and_fails_open():
    goal = {"id": 1, "metric": "score", "direction": "maximize", "target": "",
            "measured_by": "external evaluator"}

    async def good(digest):
        assert "ATTEMPTS SO FAR" in digest and "FINAL ANSWER" in digest
        return {"value": "0.772", "verdict": "advanced", "diagnosis": "d",
                "next_step": "n"}
    out = asyncio.run(goals_mod.judge_outcome(goal, [], "answer", decide_fn=good))
    assert out["verdict"] == "advanced" and out["value"] == "0.772"

    async def garbage(digest):
        return {"verdict": "sideways"}
    assert asyncio.run(goals_mod.judge_outcome(
        goal, [], "a", decide_fn=garbage))["verdict"] == "unknown"

    async def crash(digest):
        raise RuntimeError("x")
    out = asyncio.run(goals_mod.judge_outcome(goal, [], "a", decide_fn=crash))
    assert out["verdict"] == "unknown"            # fail-open: never breaks delivery


def test_pursuit_decision_fails_closed():
    goal = {"id": 1, "metric": "score", "direction": "maximize", "target": ""}

    async def crash(digest):
        raise RuntimeError("x")
    d = asyncio.run(goals_mod.next_iteration_decision(
        goal, [], attempts_left=3, decide_fn=crash))
    assert d["action"] == "stop"                  # a judgment failure spawns NO runs

    async def garbage(digest):
        return {"action": "explode"}
    assert asyncio.run(goals_mod.next_iteration_decision(
        goal, [], attempts_left=3, decide_fn=garbage))["action"] == "stop"


def test_compose_next_objective_forces_research_after_regression():
    goal = {"id": 4, "metric": "score", "direction": "maximize", "target": "0.8"}
    obj = goals_mod.compose_next_objective(goal, "regressed", "0.772", "try approach Z")
    assert "standing goal #4" in obj
    assert "RESEARCH" in obj and "before building" in obj   # the mandated first step
    assert "try approach Z" in obj
    obj2 = goals_mod.compose_next_objective(goal, "advanced", "0.772", "")
    assert "RESEARCH stage into why" not in obj2            # only regressions force it


# --- credential invalidation on repeated verify failure -------------------------- #
def test_stale_credential_invalidated_and_recollected(tmp_path, monkeypatch):
    """A cached wrong-shaped credential must not be silently re-injected
    forever: after CREDENTIAL_REVERIFY_THRESHOLD verify failures the value is
    invalidated, re-collected fresh, and the fresh value opens a new epoch."""
    monkeypatch.setattr(config, "CREDENTIAL_REVERIFY_THRESHOLD", 2)
    monkeypatch.setattr(acquisition, "_generic_verify",
                        lambda *a, **k: (False, "auth rejected", None))
    reg = Registry(tmp_path / "r.db", tmp_path / "store")

    class _Broker:
        def __init__(self):
            self.secrets = {"TOK": "stale-value"}
            self.collected = 0
            self.invalidations = 0
        def invalidate(self, name, reason=""):
            if self.secrets.pop(name, None) is not None:
                self.invalidations += 1
                return True
            return False
        def request(self, **kw):
            return True
        def collect_secret(self, name, purpose="", help_text=None):
            self.collected += 1
            self.secrets[name] = f"fresh-{self.collected}"   # real broker stores it
            return self.secrets[name]

    class _Mgr:
        def build_image(self, tag, ctx):
            pass

    broker = _Broker()

    async def dev(need_key, description, previous=None, error=None):
        return {"program": "p", "dockerfile": "FROM python:3.12-slim",
                "command": ["python", "-B", "/app/tool.py"],
                "input_schema": {"type": "object", "properties": {}},
                "env_map": {}, "network": "none", "sample_input": {},
                "requires_credential": "TOK", "side_effects": "external_read"}

    try:
        asyncio.run(acquisition._author_capability(
            "cred_probe", "a tool", manager=_Mgr(), registry=reg, broker=broker,
            model=None, developer=dev, max_attempts=5, tracer=NullTracer()))
    except AcquisitionError as e:
        h = "\n".join(e.history)
        assert "credential TOK invalidated" in h
        # The counter RESETS after invalidation: a second stale value is also
        # invalidated (the once-per-name bug the review caught).
        assert broker.invalidations == 2 and broker.collected == 2
        assert "TOK became available" in h        # fresh value opened a new epoch
    else:
        raise AssertionError("expected AcquisitionError")
    finally:
        reg.close()


# --- side effects: declaration, dry-run verify ----------------------------------- #
def test_external_write_verifies_in_dry_run(tmp_path):
    """A world-acting tool must never act during verification: verify injects
    BRAIN_DRY_RUN=1 and passes only on a declared dry_run output."""
    captured_env: dict = {}

    class _Res:
        ok, oom, exit_code, timed_out = True, False, 0, False
        stderr = ""
        stdout = '{"dry_run": true, "would_do": "the real action"}'
        output_files = {}

    class _Mgr:
        def run_ephemeral(self, image, command, *, policy, capability,
                          environment=None, input_files=None,
                          capture_output_files=False, **kw):
            captured_env.update(environment or {})
            return _Res()

    cap = Capability(name="acts", description="acts on a live service",
                     image_tag="x", command=["r"], input_schema={}, env_map={},
                     side_effects="external_write")
    ok, detail, _ = acquisition._generic_verify(cap, {}, {}, _Mgr())
    assert ok and "dry-run" in detail
    assert captured_env.get("BRAIN_DRY_RUN") == "1"

    class _ResReal(_Res):
        stdout = '{"submitted": true}'            # acted for real — must FAIL

    class _MgrReal(_Mgr):
        def run_ephemeral(self, *a, **k):
            return _ResReal()
    ok, detail, _ = acquisition._generic_verify(cap, {}, {}, _MgrReal())
    assert not ok and "BRAIN_DRY_RUN" in detail


def test_side_effects_declared_and_persisted(tmp_path):
    sub = {"command": ["r"], "input_schema": {}, "env_map": {},
           "network": "bridge", "side_effects": "external_write"}
    cap = acquisition._capability_from_submission("k", "d", sub)
    assert cap.side_effects == "external_write"
    assert acquisition._capability_from_submission(
        "k", "d", {**sub, "side_effects": "weird"}).side_effects == "none"
    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    reg.add(cap)
    assert reg.get("k").side_effects == "external_write"   # column roundtrip
    reg.close()
    assert "side_effects" in acquisition._SUBMIT_TOOL_SCHEMA["properties"]


def test_probes_honor_dry_run_for_external_write():
    """The dry-run invariant must hold on EVERY verification-time run, not just
    the happy-path verify — the review caught that the fabrication probes
    re-ran world-acting tools with real credentials and no BRAIN_DRY_RUN."""
    cap_write = Capability(name="w", description="acts", image_tag="x", command=["r"],
                           input_schema={"type": "object",
                                         "properties": {"q": {"type": "string"}}},
                           env_map={"q": "Q"}, side_effects="external_write")
    assert acquisition._verification_env(cap_write, {"KEY": "v"})["BRAIN_DRY_RUN"] == "1"
    cap_read = Capability(name="r", description="reads", image_tag="x", command=["r"],
                          input_schema={}, env_map={}, side_effects="external_read")
    assert "BRAIN_DRY_RUN" not in acquisition._verification_env(cap_read, {})

    captured: dict = {}

    class _Res:
        ok, oom, exit_code, timed_out = True, False, 0, False
        stderr, stdout, output_files = "", '{"dry_run": true}', {}

    class _Mgr:
        def run_ephemeral(self, image, command, *, policy, capability,
                          environment=None, input_files=None, capture_output_files=False, **kw):
            captured.update(environment or {})
            return _Res()

    # The no-input fabrication probe on an external_write cap must dry-run.
    ok, _ = acquisition._no_input_probe(
        cap_write, {"input_schema": cap_write.input_schema}, _Mgr(), {"KEY": "v"})
    assert ok and captured.get("BRAIN_DRY_RUN") == "1"


def test_world_reaching_tool_must_declare_side_effects(tmp_path):
    """A networked/credentialed submission that omits side_effects is rejected
    (mechanical resubmit) — the safety enum's unsafe value must not be the
    silent default for tools that can reach a live service."""
    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    seen: list = []

    async def dev(need_key, description, previous=None, error=None):
        seen.append(error)                              # omit side_effects always
        return {"program": "p", "dockerfile": "FROM python:3.12-slim",
                "command": ["python", "-B", "/app/tool.py"],
                "input_schema": {"type": "object", "properties": {}},
                "env_map": {}, "network": "bridge", "sample_input": {},
                "emits": "observation"}

    class _Mgr:
        def build_image(self, tag, ctx):
            pass

    try:
        asyncio.run(acquisition._author_capability(
            "netcap", "a networked tool", manager=_Mgr(), registry=reg, broker=None,
            model=None, developer=dev, max_attempts=2, tracer=NullTracer()))
    except AcquisitionError as e:
        assert "side_effects" in "\n".join(e.history)   # rejected on the declaration
    finally:
        reg.close()
    # The resubmit error delivered to the 2nd attempt names the missing field.
    assert len(seen) == 2 and "side_effects" in (seen[1] or "")
