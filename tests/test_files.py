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
