"""Capability resolver: per capability need, decide reuse vs. create vs. no-op.

Resolution is by KEY first, then by CONTRACT. The key is what the planner
named; the contract is what the capability actually accepts and emits, and the
invariant says capabilities are identified by their contract, never assumed by
name. Exact-key-only matching made the whole library reachable solely through
the planner guessing the same snake_case string twice — and when it guessed a
synonym, the engine paid a full authoring session plus an image build to
rebuild something it already had.

Semantic/embedding matching remains out of scope: contract equality is
deterministic and checkable, which a similarity score is not.
"""
from __future__ import annotations

from typing import Optional

from .models import CREATE, NOOP, REUSE, Decision
from .registry import Registry


def _accepts(cap) -> frozenset:
    return frozenset(a.lower().strip() for a in (cap.accepts or []) if a)


def _arg_names(cap) -> frozenset:
    props = (cap.input_schema or {}).get("properties") or {}
    return frozenset(k.lower() for k in props)


class Resolver:
    def __init__(self, registry: Registry):
        self.registry = registry

    def resolve(self, capability_need: Optional[str],
                contract: Optional[dict] = None) -> Decision:
        if not capability_need:
            return Decision(kind=NOOP, capability_need=None)

        cap = self.registry.get(capability_need)
        if self._trusted(cap):
            return Decision(kind=REUSE, capability_need=capability_need, capability=cap)

        # Key miss. Before paying to author, ask whether a trusted capability
        # already satisfies THIS contract. Only an exact match on the declared
        # interface counts — same accepted input kinds, same output role, and
        # the needed arguments present — so reuse stays a deterministic fact
        # rather than a judgement about names.
        if match := self._by_contract(contract):
            return Decision(kind=REUSE, capability_need=capability_need, capability=match)
        return Decision(kind=CREATE, capability_need=capability_need)

    @staticmethod
    def _trusted(cap) -> bool:
        return cap is not None and cap.verified and not cap.quarantined

    def _by_contract(self, contract: Optional[dict]):
        """The single trusted capability whose declared interface satisfies
        `contract`, or None. Ambiguity is not resolved here: if several match,
        naming one of them would be a guess, so the engine authors instead."""
        if not contract:
            return None
        want_accepts = frozenset(
            a.lower().strip() for a in (contract.get("accepts") or []) if a)
        want_emits = str(contract.get("emits") or "").strip().lower()
        want_args = frozenset(k.lower() for k in (contract.get("arguments") or []))
        if not want_emits or not (want_accepts or want_args):
            return None      # too little declared to match on

        hits = []
        for cap in self.registry.all():
            if not self._trusted(cap):
                continue
            if (cap.emits or "").strip().lower() != want_emits:
                continue
            if want_accepts and not want_accepts <= _accepts(cap):
                continue
            if want_args and not want_args <= _arg_names(cap):
                continue
            hits.append(cap)
        return hits[0] if len(hits) == 1 else None
