"""Deterministic verification: prove a built capability actually works before
it is trusted or promoted. "An agent built it" is not "it works."

Verifiers are deterministic (shape/content checks on real output), not LLM
judgments, wherever possible.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .models import Capability
from .sandbox import policy


@dataclass
class VerificationResult:
    passed: bool
    detail: str


def _last_json_line(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    # The program prints a single JSON object on the last line.
    return json.loads(text.splitlines()[-1])


def verify_web_search(cap: Capability, manager) -> VerificationResult:
    pol = policy.egress() if cap.network == "bridge" else policy.isolated()
    env = {
        cap.env_map.get("query", "SEARCH_QUERY"): "site reliability engineering",
        cap.env_map.get("max_results", "SEARCH_MAX_RESULTS"): "3",
    }
    res = manager.run_ephemeral(
        cap.image_tag, cap.command, policy=pol, capability=cap.name, environment=env, ttl=60
    )
    if not res.ok:
        return VerificationResult(False, f"non-zero exit/timeout: {res.stderr[:300]}")
    try:
        data = _last_json_line(res.stdout)
    except Exception as e:  # noqa: BLE001
        return VerificationResult(False, f"output not JSON ({e}); got: {res.stdout[:200]!r}")
    results = data.get("results")
    if not isinstance(results, list) or len(results) < 1:
        return VerificationResult(False, f"expected >=1 result, got: {data}")
    first = results[0]
    if not (isinstance(first, dict) and first.get("title") and (first.get("href") or first.get("url"))):
        return VerificationResult(False, f"first result missing title/url: {first}")
    return VerificationResult(True, f"{len(results)} results; first: {str(first.get('title'))[:60]}")


VERIFIERS = {"web_search": verify_web_search}


def verify(cap: Capability, manager) -> VerificationResult:
    fn = VERIFIERS.get(cap.name)
    if fn is None:
        return VerificationResult(False, f"no verifier registered for capability '{cap.name}'")
    return fn(cap, manager)
