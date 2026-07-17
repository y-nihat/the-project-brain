"""v0.24 — operator-provisioned credentials for unattended runs (PB-T07):
a headless campaign cannot collect a secret interactively when a tool asks,
so the operator pre-provisions it via the environment; the value enters the
broker exactly like a typed one — memory-only, name-only-traced."""
from __future__ import annotations

from brain.approval import ApprovalBroker


class RecTracer:
    def __init__(self): self.events = []
    def event(self, kind, subject, status="info", **data):
        self.events.append((kind, subject, data))


def test_preseed_value_from_env(monkeypatch):
    monkeypatch.setenv("BRAIN_SECRET_KAGGLE_API_TOKEN", "KGAT_verysecret")
    t = RecTracer()
    b = ApprovalBroker(auto="deny", tracer=t)
    loaded = b.preseed_from_env()
    assert loaded == ["KAGGLE_API_TOKEN"]
    # available to a tool that asks — even in auto-deny (pre-provisioned wins)
    assert b.collect_secret("KAGGLE_API_TOKEN", purpose="download") == "KGAT_verysecret"
    # the VALUE is never in the trace; only the name
    blob = repr(t.events)
    assert "KGAT_verysecret" not in blob
    assert "KAGGLE_API_TOKEN" in blob


def test_preseed_from_file(monkeypatch, tmp_path):
    f = tmp_path / "tok"; f.write_text("KGAT_fromfile\n")
    monkeypatch.setenv("BRAIN_SECRET_KAGGLE_API_TOKEN_FILE", str(f))
    b = ApprovalBroker(auto="deny")
    assert b.preseed_from_env() == ["KAGGLE_API_TOKEN"]
    assert b.collect_secret("KAGGLE_API_TOKEN", purpose="x") == "KGAT_fromfile"


def test_preseed_ignores_unrelated_and_empty(monkeypatch):
    monkeypatch.setenv("BRAIN_SECRET_EMPTY", "")
    monkeypatch.setenv("UNRELATED", "x")
    b = ApprovalBroker(auto="deny")
    assert b.preseed_from_env() == []


def test_preseed_does_not_override_existing(monkeypatch):
    monkeypatch.setenv("BRAIN_SECRET_K", "envval")
    b = ApprovalBroker(auto="deny")
    b.secrets["K"] = "already"
    b.preseed_from_env()
    assert b.secrets["K"] == "already"
