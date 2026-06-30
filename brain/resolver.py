"""Capability resolver: per capability need, decide reuse vs. create vs. no-op.

For M1 this is an exact lookup by canonical capability key against the registry.
Semantic / embedding-based matching is a later-phase upgrade.
"""
from __future__ import annotations

from typing import Optional

from .models import CREATE, NOOP, REUSE, Decision
from .registry import Registry


class Resolver:
    def __init__(self, registry: Registry):
        self.registry = registry

    def resolve(self, capability_need: Optional[str]) -> Decision:
        if not capability_need:
            return Decision(kind=NOOP, capability_need=None)
        cap = self.registry.get(capability_need)
        if cap is not None and cap.verified:
            return Decision(kind=REUSE, capability_need=capability_need, capability=cap)
        return Decision(kind=CREATE, capability_need=capability_need)
