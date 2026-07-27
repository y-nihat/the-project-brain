"""The workspace boundary: nothing the model names may resolve outside it.

Cross-cutting, so it lives here rather than in any one module's file. Four
sites take a path segment from an untrusted producer — the agent's `files`
argument, the tar names a container wrote, the read_file tool, and
`sample_input.files` at verification — and all four now route through the one
decision in `brain.files.contained`.

REGRESSION. Three of those sites carried a hand-copied guard; the fourth, the
ordinary tool-call path in the capability runtime, carried none. `Path(ws) / p`
does not contain `p`, it concatenates: for an ABSOLUTE `p` the workspace is
discarded outright, so `files: ["/proc/self/environ"]` read the orchestrator's
own environment — which under docker-compose holds CLAUDE_CODE_OAUTH_TOKEN —
and delivered it into a model-authored container. The three guards that did
exist compared with `str.startswith`, which also accepts a sibling directory
sharing the root's name prefix.

All pure — no Docker, no LLM, no token.
"""
from __future__ import annotations

from pathlib import Path

from brain.capabilities.runtime import _save_outputs, resolve_refs
from brain.files import contained


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "input").mkdir(parents=True, exist_ok=True)
    (ws / "input" / "data.csv").write_text("a,b\n1,2\n")
    return ws


# --- the decision itself ----------------------------------------------------

def test_contained_accepts_paths_inside_the_root(tmp_path):
    ws = _ws(tmp_path)
    assert contained(ws, "input/data.csv") == (ws / "input" / "data.csv").resolve()
    assert contained(ws, "  input/data.csv  ") == (ws / "input" / "data.csv").resolve()
    assert contained(ws, "./input/data.csv") == (ws / "input" / "data.csv").resolve()


def test_contained_refuses_absolute_paths(tmp_path):
    """THE defect: a join gives an absolute reference the whole filesystem."""
    ws = _ws(tmp_path)
    outside = tmp_path / "secret.env"
    outside.write_text("CLAUDE_CODE_OAUTH_TOKEN=not-a-real-token\n")
    assert (ws / str(outside)) == outside          # what the old code computed
    assert contained(ws, str(outside)) is None     # what it computes now
    assert contained(ws, "/proc/self/environ") is None
    assert contained(ws, "/etc/passwd") is None


def test_contained_refuses_traversal_and_empty(tmp_path):
    ws = _ws(tmp_path)
    assert contained(ws, "../secret.env") is None
    assert contained(ws, "input/../../secret.env") is None
    assert contained(ws, "") is None
    assert contained(ws, None) is None


def test_contained_refuses_a_sibling_sharing_the_root_name_prefix(tmp_path):
    """Why `str.startswith` was not the test: '/x/ws-evil/f' starts with '/x/ws'.

    Reachable only through a symlink (a literal '..' is refused earlier), which
    is also why the path is resolved BEFORE it is judged.
    """
    ws = _ws(tmp_path)
    sibling = tmp_path / "ws-evil"
    sibling.mkdir()
    (sibling / "loot.txt").write_text("stolen")
    (ws / "link").symlink_to(sibling)

    escaped = (sibling / "loot.txt").resolve()
    assert str(escaped).startswith(str(ws.resolve()))   # the old check passed it
    assert contained(ws, "link/loot.txt") is None       # this one does not


def test_contained_refuses_a_symlink_pointing_out_of_the_workspace(tmp_path):
    ws = _ws(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "key.pem").write_text("-----BEGIN NOTHING-----")
    (ws / "input" / "key.pem").symlink_to(outside / "key.pem")
    assert contained(ws, "input/key.pem") is None


# --- site 1: the agent's `files` argument (capability runtime) ---------------

def test_resolve_refs_admits_workspace_files_and_refuses_escapes(tmp_path):
    ws = _ws(tmp_path)
    outside = tmp_path / "secret.env"
    outside.write_text("CLAUDE_CODE_OAUTH_TOKEN=not-a-real-token\n")
    (ws / "link").symlink_to(tmp_path)

    resolved, unresolved = resolve_refs(ws, [
        "input/data.csv",
        str(outside),           # absolute
        "/proc/self/environ",   # absolute, the token-bearing one
        "../secret.env",        # traversal
        "link/secret.env",      # symlink out
        "input/nope.csv",       # honestly missing
    ])
    assert list(resolved) == ["input/data.csv"]
    assert resolved["input/data.csv"].read_text().startswith("a,b")
    assert len(unresolved) == 5
    # An escape is reported exactly like a miss — the engine never confirms
    # which host paths exist.
    assert "/proc/self/environ" in unresolved and "input/nope.csv" in unresolved


def test_resolve_refs_tolerates_no_refs(tmp_path):
    assert resolve_refs(_ws(tmp_path), None) == ({}, [])
    assert resolve_refs(_ws(tmp_path), []) == ({}, [])


# --- site 2: names a container chose for its /work/out files ----------------

def test_save_outputs_refuses_absolute_and_sibling_prefixed_names(tmp_path):
    ws = tmp_path / "workspace"
    (tmp_path / "filegen-evil").mkdir(parents=True)
    (ws / "filegen").mkdir(parents=True)
    (ws / "filegen" / "link").symlink_to(tmp_path / "filegen-evil")

    produced = _save_outputs(ws, "filegen", {
        "ok.txt": b"kept",
        "/etc/cron.d/pwn": b"nope",          # absolute
        "../../escape.txt": b"nope",         # traversal
        "link/loot.txt": b"nope",            # symlink to a name-prefixed sibling
    })
    assert produced == ["filegen/ok.txt"]
    assert not (tmp_path / "filegen-evil" / "loot.txt").exists()
    assert not (tmp_path / "escape.txt").exists()


# --- site 3: the read_file workspace tool -----------------------------------

def test_read_file_refuses_absolute_paths(tmp_path):
    from brain.memory.blackboard import Blackboard
    from brain.orchestrator import Orchestrator

    ws = _ws(tmp_path)
    outside = tmp_path / "secret.env"
    outside.write_text("CLAUDE_CODE_OAUTH_TOKEN=not-a-real-token\n")

    bb = Blackboard(tmp_path / "b.db", "contain")
    orch = Orchestrator(session="contain", manager=None, ledger=None, registry=None,
                        blackboard=bb, broker=None, planner=None, resolver=None)
    try:
        assert orch._read_workspace(ws, str(outside)).get("is_error")
        assert orch._read_workspace(ws, "/etc/passwd").get("is_error")
        assert orch._read_workspace(ws, "../secret.env").get("is_error")
        ok = orch._read_workspace(ws, "input/data.csv")
        assert not ok.get("is_error")
        # The leading-separator spelling still reaches the real file.
        assert not orch._read_workspace(ws, "/input/data.csv").get("is_error")
    finally:
        bb.close()


# --- the invariant stays wired ----------------------------------------------

_SITES = {
    "brain/capabilities/runtime.py": 2,   # resolve_refs + _save_outputs
    "brain/orchestrator.py": 1,           # the read_file tool
    "brain/acquisition.py": 1,            # sample_input.files at verification
}


def test_every_containment_site_routes_through_the_shared_helper():
    """A hand-maintained invariant at four sites already slipped at one.

    Fails if a site stops calling `contained`, or if a bare `startswith`
    containment check reappears anywhere in the engine.
    """
    root = Path(__file__).resolve().parent.parent
    for rel, least in _SITES.items():
        src = (root / rel).read_text()
        assert src.count("contained(") >= least, f"{rel} stopped using contained()"
    for py in sorted((root / "brain").rglob("*.py")):
        assert "startswith(str(" not in py.read_text(), (
            f"{py.relative_to(root)}: containment must use brain.files.contained, "
            "not a string-prefix comparison")


# --- the segment rule (stage ids, capability names -> filenames) ------------

def test_is_safe_segment_admits_engine_names_and_refuses_path_syntax():
    from brain.files import is_safe_segment

    for ok in ("extract_frames", "attempt-3-author", "stage1.fix", "promoted", "a"):
        assert is_safe_segment(ok), ok
    for bad in ("../../etc", "a/b", "/abs", "..", ".", "", None, ".hidden",
                "a" * 65, "x\ny", "with space"):
        assert not is_safe_segment(bad), bad


def test_safe_segment_coerces_or_falls_back():
    from brain.files import safe_segment

    assert safe_segment("extract_frames", "cap") == "extract_frames"
    assert safe_segment("../../VICTIM/evil", "cap") == "VICTIM-evil"
    assert safe_segment("/", "cap") == "cap"
    assert safe_segment("..", "cap") == "cap"
    assert safe_segment(None, "cap") == "cap"
