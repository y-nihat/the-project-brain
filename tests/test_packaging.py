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

try:
    import tomllib                      # 3.11+
except ModuleNotFoundError:             # 3.10, the declared floor
    import tomli as tomllib

import pytest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_collection_is_confined_to_the_engine_suite(pytestconfig):
    """`testpaths` must pin collection to tests/, and data/ must be pruned."""
    assert pytestconfig.getini("testpaths") == ["tests"]
    assert "data" in pytestconfig.getini("norecursedirs")


def test_the_collection_guard_is_load_bearing(pytestconfig):
    """Prove the guard matters: the run-output tree really does contain files
    that look like tests to pytest. If this ever stops being true the guard is
    still correct, but this test documents WHY it exists."""
    data = PROJECT_ROOT / "data"
    if not data.is_dir():
        return  # fresh clone: nothing produced yet, guard is pre-emptive
    stray = [p for p in data.rglob("test_*.py")]
    if not stray:
        return
    # The old loop could not fail: every path under data/ has ('data',) as its
    # first component by construction, so `'tests' not in parts[:1]` was true
    # for every possible input. What must hold is that the ini actually PRUNES
    # the directory the stray file lives in.
    pruned = set(pytestconfig.getini("norecursedirs"))
    paths = set(pytestconfig.getini("testpaths"))
    for p in stray:
        top = Path(p).relative_to(PROJECT_ROOT).parts[0]
        assert top in pruned, f"{p} lives in {top!r}, which norecursedirs does not prune"
        assert top not in paths, f"{p} lives in {top!r}, which is a testpath"
def test_packages_are_discovered_not_enumerated():
    """A hand-written package list omitted `brain.observability` and
    `brain.evals`. `brain/session.py` imports observability at module load, so
    an installed distribution raised
    `ModuleNotFoundError: No module named 'brain.observability'` on first use —
    the next milestone (a pip-installable package + `brain` CLI) started
    broken. Verified by building both configs: the enumeration shipped only
    capabilities/memory/sandbox.

    Discovery cannot fall behind; an enumeration silently does."""
    cfg = _pyproject()
    packages = cfg.get("tool", {}).get("setuptools", {}).get("packages")
    assert not isinstance(packages, list), (
        "packages are enumerated by hand; use [tool.setuptools.packages.find]")
    assert packages.get("find", {}).get("include") == ["brain*"]

    # Apply the declared include pattern to what is actually on disk. The old
    # loop asserted that names the test itself built with an f-string started
    # with 'brain' — true by construction, and blind to the very defect the
    # docstring describes. (setuptools is a BUILD dependency and is absent from
    # a lean runtime env, so the pattern is matched directly rather than by
    # importing find_packages — the semantics are the same and the check
    # always runs.)
    from fnmatch import fnmatch

    on_disk = {".".join(p.parent.relative_to(PROJECT_ROOT).parts)
               for p in (PROJECT_ROOT / "brain").rglob("__init__.py")}
    assert len(on_disk) > 1, "expected subpackages under brain/"
    patterns = packages["find"]["include"]
    unshipped = {pkg for pkg in on_disk
                 if not any(fnmatch(pkg, pat) for pat in patterns)}
    assert not unshipped, f"declared discovery would not ship: {sorted(unshipped)}"
    # ...and the pattern must not sweep run output into the distribution.
    assert not any(fnmatch("data", pat) or fnmatch("data.runs", pat)
                   for pat in patterns)


def test_one_version_source():
    """Three numbers disagreed: pyproject said 0.0.1, `ENGINE_VERSION` said
    0.27.0, and the CHANGELOG agreed with the latter. `ENGINE_VERSION` is the
    one with consequences — it is stamped onto every traced run and into every
    capability's validation epoch — so the distribution reads it, and nothing
    else declares a version."""
    from brain import config

    cfg = _pyproject()
    assert "version" not in cfg["project"], "a second, hand-maintained version"
    assert cfg["project"]["dynamic"] == ["version"]
    assert cfg["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "brain.config.ENGINE_VERSION"}

    # The release record agrees with the engine's own stamp.
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    newest = next(line for line in changelog.splitlines() if line.startswith("## ["))
    assert newest.startswith(f"## [{config.ENGINE_VERSION}]"), (
        f"CHANGELOG newest entry {newest!r} != ENGINE_VERSION {config.ENGINE_VERSION}")


def test_the_host_side_entry_point_is_declared():
    """M2's `brain` CLI. `brain.main:main` already parses argv and exits with
    the run's status, so the console script is the only missing piece."""
    cfg = _pyproject()
    assert cfg["project"]["scripts"]["brain"] == "brain.main:main"
    from brain.main import main
    assert callable(main)


def test_the_hooks_that_guard_this_repo_are_present_and_executable():
    """No hosted CI exists — deliberately, per CONTRIBUTING.md — so the hooks
    ARE the gate. A hook that is missing or not executable fails open silently,
    which is the one way this arrangement breaks without anyone noticing."""
    import os

    for name, guards in (("pre-commit", "credential-shaped strings"),
                         ("commit-msg", "Conventional Commits"),
                         ("pre-push", "the offline test suite")):
        hook = PROJECT_ROOT / ".githooks" / name
        assert hook.is_file(), f"missing hook {name} ({guards})"
        assert os.access(hook, os.X_OK), f"hook {name} is not executable"
        assert hook.read_text().startswith("#!"), f"hook {name} has no shebang"
