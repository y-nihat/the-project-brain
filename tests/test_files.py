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
        rewritten, resolved, unresolved = files_mod.resolve_local_sources(text)
        assert resolved == [] and unresolved == [], token
        assert rewritten == text, "the objective must reach the planner unchanged"


def test_an_explicitly_designated_file_still_earns_the_ask(tmp_path, monkeypatch):
    """The gate is not weakened. A marked token — backticks, or a path
    separator — always asks, and so does a plain missing input."""
    from brain import config, files as files_mod

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")

    _t, _r, backticked = files_mod.resolve_local_sources(
        "summarize `data/missing/gone.pdf`")
    assert backticked == ["data/missing/gone.pdf"]

    _t, _r, with_path = files_mod.resolve_local_sources(
        "summarize data/missing/gone.pdf")
    assert with_path == ["data/missing/gone.pdf"]

    # A backticked BARE name is a designation too — the marks are the point.
    _t, _r, bare_marked = files_mod.resolve_local_sources("summarize `gone.pdf`")
    assert bare_marked == ["gone.pdf"]

    # And an ordinary unmarked missing input still asks: one dotted segment is
    # a filename, which is exactly what the gate exists for.
    _t, _r, plain = files_mod.resolve_local_sources(
        "Analyze the attached data.csv and summarize.")
    assert plain == ["data.csv"]


def test_the_residual_limit_is_stated_not_hidden():
    """A bare TWO-segment host with no scheme is still indistinguishable from a
    filename by shape alone. Separating those needs a public-suffix list — a
    moving external dataset this engine should not carry — so the limit is
    recorded here rather than quietly assumed away."""
    from brain import files as files_mod

    assert "public-suffix" in files_mod.resolve_local_sources.__doc__ or \
        "public-suffix" in open(files_mod.__file__, encoding="utf-8").read()


def test_a_bare_filename_that_exists_is_still_picked_up(tmp_path, monkeypatch):
    """The convenience the bare-token rule exists for must survive: when the
    guess RESOLVES it is a file, and nothing about this changes."""
    from brain import config, files as files_mod

    inbox = tmp_path / "data" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "report.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "INBOX_DIR", inbox)

    text, resolved, unresolved = files_mod.resolve_local_sources("summarize report.pdf")
    assert resolved == [str((inbox / "report.pdf").resolve())]
    assert "input/report.pdf" in text and unresolved == []
