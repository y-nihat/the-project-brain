"""Credential containment: redaction rules, canary byte-scans, repo hygiene.

The canary tests are the keystone: plant secret-shaped values into every
engine-visible channel, then byte-scan everything that reached disk. They keep
"secrets never leak" a continuously proven property, not a one-time audit.
"""
from __future__ import annotations

from pathlib import Path

from brain.observability import Tracer
from brain.observability.redact import MASK, contains_secret_shape, scrub, scrub_text

# Canaries are CONSTRUCTED so no literal secret shape exists in this file —
# the pre-commit guard and the repo scan below therefore apply to it too.
CANARY = "sk-ant-" + "canary0123456789abcdef"
CANARY2 = "9998887776" + ":" + "AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKK"
JWT_LIKE = "eyJ" + "abc0123456789" + "." + "eyJ" + "def0123456789"
AWS_LIKE = "AKIA" + "IOSFODNN7EXAMPLE"


# --- scrub rules ---------------------------------------------------------------- #
def test_scrub_masks_sensitive_keys_but_not_usage_counters():
    data = {"API_KEY": "real-value", "password": "hunter2", "authorization": "Bearer x",
            "tokens_in": 1234, "tokens_out": 99, "llm.token_count.total": 42,
            "query": "normal text"}
    out = scrub(data)
    assert out["API_KEY"] == MASK and out["password"] == MASK and out["authorization"] == MASK
    assert out["tokens_in"] == 1234 and out["tokens_out"] == 99   # usage ints untouched
    assert out["query"] == "normal text"


def test_scrub_masks_secret_shapes_anywhere():
    text = f"the key is {CANARY} and the bot token {CANARY2}, plus a JWT {JWT_LIKE}"
    cleaned = scrub_text(text)
    assert CANARY not in cleaned and CANARY2 not in cleaned and JWT_LIKE not in cleaned
    assert cleaned.count(MASK) >= 3
    assert "the key is" in cleaned                       # surrounding text survives
    # Nested structures, whatever the key names are.
    out = scrub({"a": [{"note": f"see {CANARY}"}], "b": ("x", CANARY2)})
    flat = str(out)
    assert CANARY not in flat and CANARY2 not in flat


def test_contains_secret_shape():
    assert contains_secret_shape(f"verify {CANARY} please")
    assert contains_secret_shape(AWS_LIKE)
    assert not contains_secret_shape("verify my bot's identity via the API")


# --- canary through the tracer (everything that reaches disk) -------------------- #
def test_tracer_writes_are_leakproof(tmp_path):
    tracer = Tracer(tmp_path / "b.db", tmp_path / "runs")
    run = tracer.start_run(f"do a thing with {CANARY}")            # objective channel
    tracer.event("agent.tool_call", "web_search", status="ok",
                 args_preview=f"query={CANARY2}", API_KEY="direct-secret-value")
    with tracer.span("stage", "s1") as sp:
        sp.set(output_head=f"result mentions {CANARY}")
    try:
        with tracer.span("sandbox.run", "t"):
            raise RuntimeError(f"failed with {CANARY} in the message")  # error channel
    except RuntimeError:
        pass
    run.finish("failed", outcome=f"the outcome quotes {CANARY2}")   # outcome channel
    tracer.close()

    blob = (tmp_path / "b.db").read_bytes()
    for f in (tmp_path / "runs").rglob("*"):
        if f.is_file():
            blob += f.read_bytes()
    for needle in (CANARY.encode(), CANARY2.encode(), b"direct-secret-value"):
        assert needle not in blob, f"LEAK: {needle[:12]}... reached disk"
    assert MASK.encode() in blob                                    # masking actually happened


# --- repo hygiene: no tracked file carries a secret shape ------------------------ #
def test_repo_files_carry_no_secret_shapes():
    root = Path(__file__).resolve().parent.parent
    excluded = {".git", "data", "__pycache__", ".githooks", ".pytest_cache"}
    hits = []
    for p in root.rglob("*"):
        if not p.is_file() or any(part in excluded for part in p.parts):
            continue
        if p.name.startswith(".env"):        # the real secret holder + its symlink/example
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        if contains_secret_shape(text):
            hits.append(str(p.relative_to(root)))
    assert hits == [], f"secret-shaped strings in repo files: {hits}"
