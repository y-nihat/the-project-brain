"""Generic secret redaction, applied at the tracer boundary.

Every event/span payload passes through :func:`scrub` before it is written to
the trace DB, the JSONL stream, live listeners, or exports — one choke point
that protects all downstream surfaces (files, Phoenix, scorecards, terminals),
including leak paths that do not exist yet.

Two independent rules:
  * SENSITIVE KEY NAMES — a string value stored under a key whose name matches
    token/secret/password/credential/api-key/authorization is masked outright.
    (Non-string values pass: ``tokens_in=1234`` is usage, not a secret.)
  * SECRET-SHAPED STRINGS — values matching known credential formats (Anthropic
    sk-ant-, AWS AKIA, GitHub ghp_/gho_, Slack xox, JWTs, private-key blocks,
    bot-token digit:hash shapes) are masked wherever they appear, whatever the
    key is called.

Deterministic, recursive, and bounded. The mask keeps no part of the value.
"""
from __future__ import annotations

import re
from typing import Any

MASK = "•••redacted•••"

_SENSITIVE_KEY = re.compile(
    r"(token|secret|passwd|password|credential|api[_-]?key|authorization|private[_-]?key)",
    re.IGNORECASE,
)
# Keys that merely COUNT tokens (LLM usage) are not secrets.
_USAGE_KEY = re.compile(r"^(tokens?_(in|out)|.*token_count.*)$", re.IGNORECASE)

_SECRET_SHAPES = re.compile(
    "|".join([
        r"sk-ant-[A-Za-z0-9_\-]{8,}",              # Anthropic keys/tokens
        r"sk-[A-Za-z0-9]{20,}",                     # generic sk- API keys
        r"AKIA[0-9A-Z]{16}",                        # AWS access key id
        r"ghp_[A-Za-z0-9]{20,}",                    # GitHub PAT
        r"gho_[A-Za-z0-9]{20,}",
        r"xox[baprs]-[A-Za-z0-9\-]{10,}",           # Slack
        r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}",  # JWT
        r"\d{8,10}:[A-Za-z0-9_\-]{30,}",            # bot-token shape (id:hash)
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",      # PEM blocks
    ])
)


def contains_secret_shape(text: str) -> bool:
    """True when a string carries something shaped like a credential."""
    return bool(_SECRET_SHAPES.search(text or ""))


def scrub_text(s: str) -> str:
    """Mask secret-shaped substrings in free text (shape rule only)."""
    return _SECRET_SHAPES.sub(MASK, s)


_scrub_string = scrub_text  # internal alias


def scrub(value: Any, key: str = "", _depth: int = 0) -> Any:
    """Return a copy of ``value`` with secrets masked (see module docstring)."""
    if _depth > 8:
        return MASK  # absurd nesting: refuse to guess, mask it
    if isinstance(value, str):
        if key and _SENSITIVE_KEY.search(key) and not _USAGE_KEY.match(key) and value.strip():
            return MASK
        return _scrub_string(value)
    if isinstance(value, dict):
        return {k: scrub(v, key=str(k), _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, key=key, _depth=_depth + 1) for v in value]
    return value
