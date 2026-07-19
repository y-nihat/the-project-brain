"""Generality audit implementation (v0.26.0): innate needs are declared data.

The audit (docs/GENERALITY_AUDIT.md) found no task contamination, but four
places where engine code NAMED a capability instead of declaring a contract:
a name-keyed verifier registry, an inline research toolbox, a fixed
renderable-extension table, and a literal in the planner fallback. The fix is
one move applied four times — brain/bootstrap.py declares the engine's innate
needs as contracts, and the loop consumes them like any other need. These
tests pin the shape: names appear only as data keys; behavior derives from
contracts and from what the registry has learned.
"""
from __future__ import annotations

import asyncio
import types

from brain import acquisition, bootstrap, verifier
from brain.memory.blackboard import Blackboard
from brain.models import Capability
from brain.observability import NullTracer
from brain.orchestrator import Orchestrator
from brain.planner import _fallback_stages


# --- the declarations themselves ------------------------------------------------- #
def test_observation_needs_are_substantive_contracts():
    assert bootstrap.OBSERVATION_NEEDS, "the observation channel must be declared"
    for key, contract in bootstrap.OBSERVATION_NEEDS.items():
        assert key and len(contract) > 60, f"'{key}' needs a real contract, not a label"
    assert bootstrap.primary_observation_need() in bootstrap.OBSERVATION_NEEDS
    assert len(bootstrap.RENDER_CONTRACT) > 100
    assert all(e.startswith(".") for e in bootstrap.RENDERABLE_EXTS_DEFAULT)


# --- the research kit derives from the declared data ----------------------------- #
def test_research_kit_derives_from_bootstrap(tmp_path, monkeypatch):
    from brain import config

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    captured: dict = {}

    async def runner(_t, name, prompt, options):
        return "findings"

    bb = Blackboard(tmp_path / "b.db", "kit")
    orch = Orchestrator(session="kit", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=None, resolver=None,
                        session_runner=runner, tracer=NullTracer())
    orch._run_ctx = ("obj", None, tmp_path / "ws")

    async def fake_acquire(spec):
        captured["capabilities"] = list(spec.capabilities)
        return {}

    monkeypatch.setattr(orch, "_acquire_caps", fake_acquire)
    try:
        asyncio.run(orch._research("some topic"))
    finally:
        bb.close()
    assert captured["capabilities"] == list(bootstrap.OBSERVATION_NEEDS)
    # Every declared need was seeded with ITS OWN contract (no inline text).
    for key, contract in bootstrap.OBSERVATION_NEEDS.items():
        assert orch._cap_needs[key]["contract"] == contract


# --- perception learns from the registry ----------------------------------------- #
def test_renderable_exts_learn_from_promoted_renderer():
    class _Reg:
        def get(self, name):
            assert name == bootstrap.RENDER_NEED_KEY
            return Capability(name=name, description="renders", image_tag="x",
                              command=["r"], input_schema={}, env_map={},
                              accepts=[".key", "fig"])

    learned = bootstrap.renderable_exts(_Reg())
    assert ".key" in learned and ".fig" in learned          # tool-declared, normalized
    assert bootstrap.RENDERABLE_EXTS_DEFAULT <= learned     # prior always kept
    # No registry (stub harnesses) degrades to the prior.
    assert bootstrap.renderable_exts(None) == bootstrap.RENDERABLE_EXTS_DEFAULT
    # A broken lookup must never break perception.
    class _Boom:
        def get(self, name):
            raise RuntimeError("db gone")
    assert bootstrap.renderable_exts(_Boom()) == bootstrap.RENDERABLE_EXTS_DEFAULT


def test_quarantined_renderer_does_not_extend_perception():
    class _Reg:
        def get(self, name):
            return Capability(name=name, description="renders", image_tag="x",
                              command=["r"], input_schema={}, env_map={},
                              accepts=[".key"], quarantined="probe failed")

    assert ".key" not in bootstrap.renderable_exts(_Reg())


# --- planner fallback derives from the data -------------------------------------- #
def test_planner_fallback_uses_declared_observation_need():
    stages = _fallback_stages("what is X?")
    assert len(stages) == 1 and stages[0].goal == "what is X?"
    assert stages[0].capabilities == [bootstrap.primary_observation_need()]


# --- recipes carry their own shape verifier; no name-keyed registry -------------- #
def test_recipe_carries_shape_verifier_and_registry_is_gone():
    recipe = acquisition._RECIPES["web_search"]
    assert len(recipe) == 5 and callable(recipe[4])
    assert not hasattr(verifier, "VERIFIERS")
    assert not hasattr(verifier, "verify")
    # Single source of truth: the recipe capability's contract IS the
    # bootstrap declaration.
    cap = acquisition.web_search_capability()
    assert cap.description == bootstrap.OBSERVATION_NEEDS["web_search"]


# --- static recipes do not inherit the learning-loop ceiling --------------------- #
def test_recipe_attempts_clamped_below_authoring_ceiling(tmp_path, monkeypatch):
    """A recipe is static content — retries rebuild identical bytes, so the
    raised ACQUISITION_MAX_ATTEMPTS (for loops that LEARN between attempts)
    must not multiply recipe rebuilds on persistent failure."""
    from brain import config
    from brain.acquisition import AcquisitionError

    monkeypatch.setattr(config, "ACQUISITION_MAX_ATTEMPTS", 10)
    monkeypatch.setattr(acquisition, "_generic_verify",
                        lambda *a, **k: (False, "always fails", None))

    class _Mgr:
        builds = 0
        def build_image(self, tag, ctx):
            _Mgr.builds += 1

    try:
        asyncio.run(acquisition.acquire(
            "web_search", manager=_Mgr(), registry=None, broker=None,
            tracer=NullTracer()))
        raise AssertionError("expected AcquisitionError")
    except AcquisitionError as e:
        # The error reports the count actually RUN, not the pre-clamp ceiling
        # (the inflated number fed the persistence digest and failure lines).
        assert e.attempts == acquisition._RECIPE_BUILD_ATTEMPTS
    assert _Mgr.builds == acquisition._RECIPE_BUILD_ATTEMPTS  # 3, not 10


# --- bootstrap must not import the rest of the engine ---------------------------- #
def test_bootstrap_is_a_leaf_module():
    import brain.bootstrap as b

    engine_imports = [m for m in vars(b).values()
                      if isinstance(m, types.ModuleType)
                      and getattr(m, "__name__", "").startswith("brain.")]
    assert engine_imports == [], "bootstrap is declarations only — no engine imports"
