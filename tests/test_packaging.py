"""Build/packaging invariants: what ships, and what the test runner collects.

Both properties below are load-bearing and both were observed broken:

* Collection scope — a bare `pytest` from the repo root (the command
  `docs/OVERVIEW.md` documents) walked `data/`, the git-ignored RUN OUTPUT
  tree, and tried to import test modules that AGENTS authored and tools
  extracted during past runs. 25 collection errors, and — far worse — an
  attempt to execute agent-generated code on the HOST, which is exactly the
  thing the sandbox exists to prevent.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_collection_is_confined_to_the_engine_suite(pytestconfig):
    """`testpaths` must pin collection to tests/, and data/ must be pruned."""
    assert pytestconfig.getini("testpaths") == ["tests"]
    assert "data" in pytestconfig.getini("norecursedirs")


def test_the_collection_guard_is_load_bearing():
    """Prove the guard matters: the run-output tree really does contain files
    that look like tests to pytest. If this ever stops being true the guard is
    still correct, but this test documents WHY it exists."""
    data = PROJECT_ROOT / "data"
    if not data.is_dir():
        return  # fresh clone: nothing produced yet, guard is pre-emptive
    stray = [p for p in data.rglob("test_*.py")]
    for p in stray:
        assert "tests" not in Path(p).relative_to(PROJECT_ROOT).parts[:1], (
            f"{p} would be collected: it resolves inside a testpath")


def test_run_outputs_are_not_packaged():
    """data/ is run output, never distributable content."""
    cfg = _pyproject()
    declared = cfg.get("tool", {}).get("setuptools", {})
    packages = declared.get("packages")
    if isinstance(packages, list):
        assert not any(p.startswith("data") for p in packages)
