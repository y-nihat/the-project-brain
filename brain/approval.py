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
    ):
        if auto not in (None, "approve", "deny"):
            raise ValueError("auto must be None, 'approve', or 'deny'")
        self.auto = auto
        self._input = input_fn
        self._output = output_fn
        self.log: list[tuple[ApprovalRequest, bool]] = []

    def request(self, action: str, reason: str, requirements: Iterable[str] = ()) -> bool:
        req = ApprovalRequest(action=action, reason=reason, requirements=list(requirements))
        approved = self._decide(req)
        self.log.append((req, approved))
        return approved

    def require(self, action: str, reason: str, requirements: Iterable[str] = ()) -> None:
        """Like :meth:`request` but raises :class:`ApprovalDenied` on refusal."""
        if not self.request(action, reason, requirements):
            raise ApprovalDenied(action)

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
