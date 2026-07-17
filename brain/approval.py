"""Approval gate + deny-by-default permission callback.

Two related pieces, kept in one module because they are the system's trust
boundary:

1. ``ApprovalBroker`` — the single interactive channel through which the
   *orchestrator* requests human sign-off for anything that has no free path or
   needs a credential, payment, account, or irreversible external action. It
   blocks until the human answers. Invoked directly by orchestrator code
   (resolver, acquisition), not by agents. Pure-Python; importable without the
   SDK installed.

2. ``make_can_use_tool`` / ``keepalive_hooks`` — a deny-by-default
   ``can_use_tool`` callback (plus the PreToolUse keepalive hook the Python SDK
   requires) for agent sessions. Allow-listed capability tools are approved;
   anything else reaching the callback is denied and logged. This is the runtime
   guarantee that an agent can never execute anything off-policy.
"""
from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


@dataclass
class ApprovalRequest:
    action: str               # what the brain wants to do
    reason: str               # why it cannot proceed freely/automatically
    requirements: list[str] = field(default_factory=list)  # e.g. ["API key", "paid plan"]


class ApprovalDenied(RuntimeError):
    """Raised when the human declines an approval request and the caller treats
    a denial as fatal for that step."""


class ApprovalBroker:
    """Interactive, blocking approval gate.

    Defaults to prompting on stdin. For tests/automation, inject ``input_fn`` or
    set ``auto`` to ``"approve"`` / ``"deny"``.
    """

    def __init__(
        self,
        *,
        auto: Optional[str] = None,  # None | "approve" | "deny"
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
        secret_input_fn: Optional[Callable[[str], str]] = None,  # None => getpass (hidden)
        tracer=None,
    ):
        if auto not in (None, "approve", "deny"):
            raise ValueError("auto must be None, 'approve', or 'deny'")
        self.auto = auto
        self._input = input_fn
        self._output = output_fn
        self._secret_input = secret_input_fn
        from .observability import NullTracer

        self.tracer = tracer or NullTracer()
        self.log: list[tuple[ApprovalRequest, bool]] = []
        # Session-only secret store: {ENV_VAR_NAME: value}. Values are injected
        # into sandbox environments at run time and are NEVER written to the
        # trace, the registry, or disk.
        self.secrets: dict[str, str] = {}

    def preseed_from_env(self) -> list[str]:
        """Load operator-provisioned credentials from the environment so an
        UNATTENDED run can use a secret a human would otherwise type when a tool
        asks. Two forms per credential NAME:

          * ``BRAIN_SECRET_<NAME>``       — the value directly, or
          * ``BRAIN_SECRET_<NAME>_FILE``  — a path whose file CONTENTS are the
            value (preferred: keeps the secret off every command line).

        The value enters :attr:`secrets` exactly like an interactively-collected
        one — memory-only, injected into sandbox env at call time, and NEVER
        written to the trace/registry/disk; only the NAME is recorded. Returns
        the names loaded (for the caller to log by NAME only)."""
        import os

        loaded: list[str] = []
        for key, val in list(os.environ.items()):
            if not key.startswith("BRAIN_SECRET_") or not val:
                continue
            if key.endswith("_FILE"):
                name = key[len("BRAIN_SECRET_"):-len("_FILE")]
                try:
                    value = pathlib.Path(val).read_text().strip()
                except OSError:
                    continue
            else:
                name = key[len("BRAIN_SECRET_"):]
                value = val.strip()
            if name and value and name not in self.secrets:
                self.secrets[name] = value
                loaded.append(name)
        if loaded:
            self.tracer.event("secret.preseeded", "operator", status="info",
                              names=sorted(loaded))  # NAMES only — never values
        return sorted(loaded)

    def request(self, action: str, reason: str, requirements: Iterable[str] = ()) -> bool:
        req = ApprovalRequest(action=action, reason=reason, requirements=list(requirements))
        self.tracer.event(
            "approval.requested", action[:60], status="info",
            action=action, reason=reason, requirements=list(requirements),
        )
        approved = self._decide(req)
        self.tracer.event(
            "approval.resolved", action[:60],
            status="granted" if approved else "denied",
            outcome="granted" if approved else "denied",
        )
        self.log.append((req, approved))
        return approved

    def require(self, action: str, reason: str, requirements: Iterable[str] = ()) -> None:
        """Like :meth:`request` but raises :class:`ApprovalDenied` on refusal."""
        if not self.request(action, reason, requirements):
            raise ApprovalDenied(action)

    def inform(self, text: str) -> None:
        """Show the user something NOW (used by the supervisor when an upcoming
        interaction needs context first). Traced by length/preview only."""
        text = (text or "").strip()
        if not text:
            return
        self.tracer.event("user.informed", "supervisor", status="info",
                          chars=len(text), preview=text[:200])
        self._output("\n  ℹ  " + "\n  ".join(text.splitlines()))

    def collect_secret(self, name: str, purpose: str,
                       help_text: Optional[str] = None) -> Optional[str]:
        """Interactively collect a credential VALUE (e.g. an API key).

        ``help_text`` — instructions for OBTAINING the credential — is shown
        BEFORE the prompt: whoever asks a human for a key must first tell them
        where to find it. The value is cached for this session under ``name``
        and injected into sandbox environments by the runtime; only the NAME is
        ever traced. Returns None when the user provides nothing (or on
        auto-deny)."""
        if name in self.secrets:
            return self.secrets[name]
        self.tracer.event("secret.requested", name, status="info", purpose=purpose[:200],
                          help_shown=bool(help_text))

        if self.auto == "deny":
            value = ""
        elif self.auto == "approve":
            value = f"auto-secret-{name}"   # tests/automation: a stand-in value
        else:
            self._output(
                f"\n  SECRET NEEDED — {purpose}\n"
                f"  The value of {name} is held in memory for THIS session only; "
                "it is never logged, traced, or stored on disk.\n"
                "  (leave empty to refuse)"
            )
            if help_text:
                self._output(
                    "  HOW TO OBTAIN " + name + ":\n"
                    + "\n".join(f"    {ln}" for ln in str(help_text).strip().splitlines())
                )
            hidden = self._secret_input is None
            if hidden:
                import getpass

                fn = getpass.getpass
            else:
                fn = self._secret_input
            while True:
                prompt = (f"  Enter {name} (input hidden — type show to switch to visible typing): "
                          if hidden else f"  Enter {name}: ")
                try:
                    value = (fn(prompt) or "").strip()
                except Exception:  # noqa: BLE001 — no tty, EOF, interrupted, ...
                    value = ""
                if hidden and value.lower() == "show":
                    try:
                        value = (self._input(f"  Enter {name} (VISIBLE on screen): ") or "").strip()
                    except Exception:  # noqa: BLE001
                        value = ""
                if not value or not hidden:
                    break
                # Hidden input is unverifiable by eye: echo a masked receipt so a
                # bad paste is caught HERE, not as a cryptic API failure later.
                masked = f"{value[:4]}…{value[-4:]}" if len(value) > 12 else "•" * len(value)
                try:
                    ans = (self._input(
                        f"  Received {len(value)} characters: {masked} — use this? (Y/n): ")
                        or "").strip().lower()
                except Exception:  # noqa: BLE001
                    ans = ""
                if ans in ("", "y", "yes"):
                    break
                self._output("  OK — let's try again.")

        if not value:
            self.tracer.event("secret.provided", name, status="denied")
            return None
        self.secrets[name] = value
        self.tracer.event("secret.provided", name, status="granted")  # name only, never the value
        return value

    def collect_value(self, name: str, purpose: str,
                      help_text: Optional[str] = None) -> Optional[str]:
        """Interactively collect a NON-secret value from the user — e.g. a
        corrected file path when a designated source cannot be reached. Unlike
        :meth:`collect_secret` the value is shown on screen and is NOT stored in
        the session secret store; it is the same interactive channel the
        credential gate uses, extended to 'the system needs a fact only you can
        supply'. Returns None on empty input or in an automated session."""
        self.tracer.event("input.requested", name[:60], status="info", purpose=purpose[:200])
        if self.auto is not None:
            # Automated/non-interactive sessions cannot answer a free-form ask.
            self.tracer.event("input.provided", name[:60], status="denied")
            return None
        self._output(
            f"\n  INPUT NEEDED — {purpose}\n"
            + (("  " + "\n  ".join(str(help_text).strip().splitlines()) + "\n")
               if help_text else "")
            + "  (leave empty to proceed without it)"
        )
        try:
            value = (self._input(f"  Enter {name}: ") or "").strip()
        except Exception:  # noqa: BLE001 — no tty, EOF, interrupted, ...
            value = ""
        self.tracer.event("input.provided", name[:60],
                          status="granted" if value else "denied")
        return value or None

    def _decide(self, req: ApprovalRequest) -> bool:
        banner = (
            "\n" + "=" * 72 + "\n"
            "  APPROVAL REQUIRED — the brain has stopped and needs your decision\n"
            + "=" * 72 + "\n"
            f"  I plan to: {req.action}\n"
            f"  This needs your approval because: {req.reason}\n"
        )
        if req.requirements:
            banner += f"  It requires: {', '.join(req.requirements)}\n"
        banner += "=" * 72
        self._output(banner)

        if self.auto == "approve":
            self._output("  [auto-approve] proceeding.")
            return True
        if self.auto == "deny":
            self._output("  [auto-deny] not proceeding.")
            return False

        try:
            answer = self._input("  Approve? (y/N): ").strip().lower()
        except EOFError:
            answer = ""
        return answer in ("y", "yes")


# --------------------------------------------------------------------------- #
# Agent-session permission plumbing (SDK-dependent; imported lazily so the
# broker above stays usable without claude-agent-sdk installed).
# --------------------------------------------------------------------------- #

def make_can_use_tool(
    allowed_tools: Iterable[str],
    *,
    on_deny: Optional[Callable[[str, dict], None]] = None,
):
    """Build a deny-by-default ``can_use_tool`` callback for an agent session.

    Tools in ``allowed_tools`` are also pre-approved by the SDK's allow rules, so
    in practice this callback is the catch-all that denies anything unexpected
    that still reaches it.
    """
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    allowed = set(allowed_tools)

    async def can_use_tool(tool_name: str, input_data: dict, context):  # noqa: ANN001
        if tool_name in allowed:
            return PermissionResultAllow(updated_input=input_data)
        if on_deny is not None:
            on_deny(tool_name, input_data)
        return PermissionResultDeny(
            message=(
                f"'{tool_name}' is not in this agent's allow-list. The brain's "
                "deny-by-default policy blocks it. Use only your provided capabilities."
            )
        )

    return can_use_tool


def keepalive_hooks():
    """PreToolUse hook map that keeps the Python SDK stream open so
    ``can_use_tool`` can fire (a documented requirement)."""
    from claude_agent_sdk.types import HookMatcher

    async def _keepalive(input_data, tool_use_id, context):  # noqa: ANN001
        return {"continue_": True}

    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[_keepalive])]}
