"""User-provided files: mention parsing, materialization, caps — all pure."""
from __future__ import annotations

import pytest

from brain import config, files


def test_parse_mentions_rewrites_to_workspace_names():
    clean, paths = files.parse_mentions(
        'analyze @data/inbox/q3 report.pdf and @"data/inbox/with space.txt" now @notes.md')
    # Unquoted mention stops at whitespace; quoted keeps spaces.
    assert paths == ["data/inbox/q3", "data/inbox/with space.txt", "notes.md"]
    assert "input/q3" in clean and "input/with space.txt" in clean and "input/notes.md" in clean
    assert "@" not in clean


def test_parse_mentions_strips_sentence_punctuation():
    # The exact live failure: "@x.pdf, summarize" swallowed the comma.
    clean, paths = files.parse_mentions(
        "Analyze @data/inbox/claude-ebook.pdf, summarize it (see @notes.md).")
    assert paths == ["data/inbox/claude-ebook.pdf", "notes.md"]
    assert "input/claude-ebook.pdf," in clean          # punctuation stays in the sentence
    assert "input/notes.md)." in clean


def test_parse_mentions_no_mentions_is_identity():
    text = "summarize the report in two sentences"
    assert files.parse_mentions(text) == (text, [])


def test_materialize_copies_with_manifest_and_collision_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "doc.txt").write_text("alpha")
    (tmp_path / "b" / "doc.txt").write_text("beta")
    ws = tmp_path / "ws"

    manifest = files.materialize(["a/doc.txt", "b/doc.txt", "a/doc.txt"], ws)
    names = [m["path"] for m in manifest]
    assert names[0] == "input/doc.txt"
    assert names[1] == "input/doc-2.txt"          # different content -> suffixed
    assert names[2] == "input/doc.txt"            # identical content -> reused
    assert (ws / "input" / "doc-2.txt").read_text() == "beta"


def test_materialize_enforces_caps_and_existence(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "FILE_ATTACH_MAX_MB", 1)
    (tmp_path / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    with pytest.raises(ValueError, match="cap"):
        files.materialize(["big.bin"], tmp_path / "ws")
    with pytest.raises(ValueError, match="not found"):
        files.materialize(["missing.pdf"], tmp_path / "ws")


def test_manifest_note_names_paths_and_mechanism():
    note = files.manifest_note([{"path": "input/r.pdf", "bytes": 1024 * 1024}])
    assert "input/r.pdf" in note and "`files` argument" in note and "1.0 MB" in note


def test_a_host_shaped_token_is_not_read_as_a_designated_file(tmp_path, monkeypatch):
    """`name.ext` is also the shape of a hostname, and every TLD matched the
    engine's file-extension rule. An objective naming a domain therefore bought
    two interrogations for a path that never existed, and then instructed every
    agent to report a fatal blocker on it — a legitimate turn broken by the
    engine's own guess.
    """
    from brain import config, files as files_mod

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")

    for token in ("shop.kelvane.com", "api.github.io", "example.co.uk",
                  "registry.npmjs.org", "cdn.jsdelivr.net"):
        text = f"deploy the site and point it at {token} when it is live"
        rewritten, resolved, unresolved, _g = files_mod.resolve_local_sources(text)
        assert resolved == [] and unresolved == [], token
        assert rewritten == text, "the objective must reach the planner unchanged"


def test_an_explicitly_designated_file_still_earns_the_ask(tmp_path, monkeypatch):
    """The gate is not weakened. A marked token — backticks, or a path
    separator — always asks, and so does a plain missing input."""
    from brain import config, files as files_mod

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")

    _t, _r, backticked, _g = files_mod.resolve_local_sources(
        "summarize `data/missing/gone.pdf`")
    assert backticked == ["data/missing/gone.pdf"]

    _t, _r, with_path, _g = files_mod.resolve_local_sources(
        "summarize data/missing/gone.pdf")
    assert with_path == ["data/missing/gone.pdf"]

    # A backticked BARE name is a designation too — the marks are the point.
    _t, _r, bare_marked, _g = files_mod.resolve_local_sources("summarize `gone.pdf`")
    assert bare_marked == ["gone.pdf"]

    # A single-segment name is a filename by any reading and keeps the gate.
    _t, _r, plain, _g = files_mod.resolve_local_sources(
        "Analyze the attached data.csv and summarize.")
    assert plain == ["data.csv"]


def test_an_unmarked_multi_segment_token_is_stated_not_swallowed(tmp_path, monkeypatch):
    """Shape cannot separate `archive.tar.gz` from `shop.example.com`, so the
    engine stops asking shape to decide and makes the CONSEQUENCE proportional:
    an unmarked multi-segment token it cannot resolve is reported as a GUESS —
    nobody is interrogated, no fatal blocker is demanded, and nothing is
    dropped in silence. An earlier version of this fix dropped them quietly,
    which replaced a loud wrong answer with a quiet one."""
    from brain import config, files as files_mod

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")

    for token in ("archive.tar.gz", "shop.kelvane.com", "data.2024.csv"):
        _t, _r, unresolved, guessed = files_mod.resolve_local_sources(
            f"handle {token} next")
        assert unresolved == [], token          # nobody is asked about a guess
        assert guessed == [token], token        # but it is on the record

    # Marking it turns the guess back into a designation.
    _t, _r, marked, guessed = files_mod.resolve_local_sources("handle `archive.tar.gz`")
    assert marked == ["archive.tar.gz"] and guessed == []


def test_the_residual_limit_is_demonstrated_not_asserted(tmp_path, monkeypatch):
    """A bare TWO-segment host with no scheme is still indistinguishable from a
    filename by shape alone — separating those needs a public-suffix list, a
    moving external dataset this engine should not carry.

    Demonstrated, not claimed: this asserts the CURRENT behaviour at the
    boundary, so the day someone closes the gap the test says so instead of
    passing on a code comment. (It replaces a check for the string
    'public-suffix' in the module text, which was unfalsifiable — and was added
    in a series whose two preceding commits deleted source-inspection tests for
    exactly that reason.)
    """
    from brain import config, files as files_mod

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")

    # Two segments: still read as a filename, and still asked about.
    _t, _r, unresolved, guessed = files_mod.resolve_local_sources("deploy to kelvane.com")
    assert unresolved == ["kelvane.com"] and guessed == []
    # Three: reported as a guess, and nobody is interrogated.
    _t, _r, unresolved, guessed = files_mod.resolve_local_sources("deploy to shop.kelvane.com")
    assert unresolved == [] and guessed == ["shop.kelvane.com"]


def test_a_bare_filename_that_exists_is_still_picked_up(tmp_path, monkeypatch):
    """The convenience the bare-token rule exists for must survive: when the
    guess RESOLVES it is a file, and nothing about this changes."""
    from brain import config, files as files_mod

    inbox = tmp_path / "data" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "report.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "INBOX_DIR", inbox)

    text, resolved, unresolved, _g = files_mod.resolve_local_sources("summarize report.pdf")
    assert resolved == [str((inbox / "report.pdf").resolve())]
    assert "input/report.pdf" in text and unresolved == []
