"""Credential gate: interactive secret collection, storage, and injection.

Pure tests plus one Docker test that authors a credentialed tool end-to-end
(gate -> collect -> verify WITH the secret -> promote with the field persisted).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from brain import acquisition, config
from brain.approval import ApprovalBroker
from brain.models import REUSE, Capability, Decision, StageSpec
from brain.registry import Registry

try:
    import docker
except Exception:  # noqa: BLE001
    docker = None


def _docker_available() -> bool:
    if docker is None:
        return False
    try:
        c = docker.DockerClient(base_url=config.DOCKER_HOST) if config.DOCKER_HOST else docker.from_env()
        c.ping(); c.close(); return True
    except Exception:  # noqa: BLE001
        return False


DOCKER = _docker_available()


# --- broker ------------------------------------------------------------------ #
def test_collect_secret_prompts_hidden_and_caches():
    prompts: list[str] = []

    def fake_secret_input(prompt: str) -> str:
        prompts.append(prompt)
        return "tok-12345"

    b = ApprovalBroker(secret_input_fn=fake_secret_input)
    assert b.collect_secret("TELEGRAM_BOT_TOKEN", "bot capability") == "tok-12345"
    assert b.secrets == {"TELEGRAM_BOT_TOKEN": "tok-12345"}
    # Cached: asking again does not re-prompt.
    assert b.collect_secret("TELEGRAM_BOT_TOKEN", "again") == "tok-12345"
    assert len(prompts) == 1
    assert "TELEGRAM_BOT_TOKEN" in prompts[0]


def test_collect_secret_refusal_and_auto_modes():
    b = ApprovalBroker(secret_input_fn=lambda _p: "")
    assert b.collect_secret("X_TOKEN", "x") is None
    assert "X_TOKEN" not in b.secrets

    assert ApprovalBroker(auto="deny").collect_secret("X", "x") is None
    v = ApprovalBroker(auto="approve").collect_secret("X", "x")
    assert v and "X" in v  # deterministic stand-in for automation


def test_secret_value_never_traced(tmp_path):
    from brain.observability import Tracer

    tracer = Tracer(tmp_path / "b.db", tmp_path / "runs")
    tracer.start_run("secret test")
    b = ApprovalBroker(secret_input_fn=lambda _p: "SUPER-SECRET-VALUE", tracer=tracer)
    b.collect_secret("API_KEY", "test tool")
    tracer.close()

    everything = (tmp_path / "b.db").read_bytes()
    for f in (tmp_path / "runs").rglob("*"):
        if f.is_file():
            everything += f.read_bytes()
    assert b"SUPER-SECRET-VALUE" not in everything
    assert b"API_KEY" in everything            # the NAME is traced


# --- registry persistence ----------------------------------------------------- #
def test_registry_persists_requires_credential(tmp_path):
    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    reg.add(Capability(name="tg_send", description="send telegram msg", image_tag="i:1",
                       command=["x"], input_schema={}, env_map={},
                       requires_credential="TELEGRAM_BOT_TOKEN", verified=True))
    got = reg.get("tg_send")
    reg.close()
    assert got.requires_credential == "TELEGRAM_BOT_TOKEN"


# --- orchestrator: reuse of a credentialed tool ------------------------------- #
class _StubResolver:
    def __init__(self, cap):
        self.cap = cap

    def resolve(self, need):
        return Decision(kind=REUSE, capability_need=need, capability=self.cap)


def test_reuse_prompts_for_missing_secret_or_skips(tmp_path, monkeypatch):
    from brain.memory.blackboard import Blackboard
    from brain.models import AgentSpec
    from brain.orchestrator import Orchestrator

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    cap = Capability(name="tg_send", description="d", image_tag="i:1", command=["x"],
                     input_schema={}, env_map={}, requires_credential="TG_TOKEN", verified=True)

    def orch(broker):
        bb = Blackboard(tmp_path / f"b-{id(broker)}.db", "s")
        return Orchestrator(session="s", manager=None, ledger=None, registry=None,
                            blackboard=bb, broker=broker, planner=None,
                            resolver=_StubResolver(cap), session_runner=None), bb

    # Approved: the secret is collected once and the tool is available.
    broker = ApprovalBroker(input_fn=lambda _p: "y", secret_input_fn=lambda _p: "tok-1")
    o, bb = orch(broker)
    caps = asyncio.run(o._acquire_caps(AgentSpec(id="a", role="r", goal="g",
                                                 capabilities=["tg_send"])))
    bb.close()
    assert "tg_send" in caps and broker.secrets == {"TG_TOKEN": "tok-1"}

    # Denied: the tool is skipped (agent proceeds without it), nothing raises.
    broker2 = ApprovalBroker(auto="deny")
    o2, bb2 = orch(broker2)
    caps2 = asyncio.run(o2._acquire_caps(AgentSpec(id="a", role="r", goal="g",
                                                   capabilities=["tg_send"])))
    bb2.close()
    assert caps2 == {} and broker2.secrets == {}


# --- verification env: the real secret must always win ------------------------ #
class _CapturingManager:
    def __init__(self, stdout='{"sent": true}'):
        self.env = None
        self.stdout = stdout

    def run_ephemeral(self, image, command, *, policy, capability, environment=None,
                      input_files=None, capture_output_files=False):
        from types import SimpleNamespace

        self.env = dict(environment or {})
        return SimpleNamespace(ok=True, oom=False, exit_code=0, timed_out=False,
                               stdout=self.stdout, stderr="", output_files={},
                               error_note=None)


def test_verify_injected_secret_beats_sample_placeholder():
    """A placeholder credential smuggled into sample_input must not shadow the
    real injected secret (the exact bug behind the Telegram 401s)."""
    mgr = _CapturingManager()
    cap = Capability(name="t", description="d", image_tag="i:1", command=["x"],
                     input_schema={}, env_map={"token": "BOT_TOKEN"})
    ok, detail, _ = acquisition._generic_verify(
        cap, {"token": "dummy-placeholder"}, {}, mgr, {"BOT_TOKEN": "real-token"})
    assert ok
    assert mgr.env["BOT_TOKEN"] == "real-token"


def test_verify_rejects_error_payload_even_with_extra_keys():
    """{"error": ..., "helpful_extras": ...} with exit 0 is still a failure."""
    mgr = _CapturingManager(stdout='{"error": "401 Unauthorized", "how_to": "..."}')
    cap = Capability(name="t", description="d", image_tag="i:1", command=["x"],
                     input_schema={}, env_map={})
    ok, detail, _ = acquisition._generic_verify(cap, {}, {}, mgr, None)
    assert not ok and "401" in detail and "env vars provided" in detail


# --- Docker: author a credentialed tool end-to-end ---------------------------- #
@pytest.mark.skipif(not DOCKER, reason="Docker daemon not reachable")
def test_author_credentialed_tool_collects_and_verifies_with_secret(tmp_path):
    from brain.ledger import Ledger
    from brain.observability import Tracer
    from brain.sandbox.manager import SandboxManager

    reg = Registry(tmp_path / "r.db", tmp_path / "store")
    ledger = Ledger(tmp_path / "l.db")
    tracer = Tracer(tmp_path / "t.db", tmp_path / "runs")
    tracer.start_run("credentialed authoring")
    mgr = SandboxManager("secret-test", ledger, tracer=tracer)
    secret_prompts: list[str] = []

    def secret_input(prompt: str) -> str:
        secret_prompts.append(prompt)
        return "tok-abc"

    broker = ApprovalBroker(input_fn=lambda _p: "y",
                            secret_input_fn=secret_input, tracer=tracer)

    # The tool FAILS without the credential — verification can only pass if the
    # engine injected the collected secret into the verify environment.
    program = (
        "import json, os\n"
        "tok = os.environ.get('FAKE_SERVICE_TOKEN')\n"
        "print(json.dumps({'error': 'no token'} if not tok else {'sent': True}))\n"
    )
    dockerfile = "FROM python:3.12-slim\nWORKDIR /app\nCOPY tool.py /app/tool.py\n"

    async def stub_developer(need_key, description, previous=None, error=None):
        # Deliberately MESSY credential naming: the engine must canonicalize it
        # to FAKE_SERVICE_TOKEN (the name the program reads) and ask only once.
        return {"program": program, "dockerfile": dockerfile,
                "command": ["python", "-B", "/app/tool.py"],
                "input_schema": {"type": "object"}, "env_map": {}, "network": "none",
                "sample_input": {}, "requires_credential": "fake service token"}

    try:
        cap = asyncio.run(acquisition.acquire(
            "fake_service_send", manager=mgr, registry=reg, broker=broker,
            developer=stub_developer, description="send via fake service", tracer=tracer))
        assert cap.verified
        assert cap.requires_credential == "FAKE_SERVICE_TOKEN"   # canonicalized
        assert broker.secrets == {"FAKE_SERVICE_TOKEN": "tok-abc"}
        assert len(secret_prompts) == 1                          # asked exactly once
        # Persisted on the promoted capability for future-session reuse prompts.
        assert reg.get("fake_service_send").requires_credential == "FAKE_SERVICE_TOKEN"
        # The secret VALUE appears nowhere in the registry store or artifacts.
        blob = b""
        for f in list((tmp_path / "store").rglob("*")) + list((tmp_path / "runs").rglob("*")):
            if f.is_file():
                blob += f.read_bytes()
        assert b"tok-abc" not in blob
        assert ledger.find_leaks("secret-test") == []
    finally:
        mgr.close(); ledger.close(); reg.close(); tracer.close()
