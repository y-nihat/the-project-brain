"""The behavior scenario suite.

Generic on purpose: each scenario probes one band of the effort spectrum, so a
regression in planning judgment, tool use, fan-out, or honesty shows up as a
flipped check — regardless of topic. Edit freely; `validate()` guards against
referencing checks that do not exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .checks import CHECKS


@dataclass
class Scenario:
    id: str
    objective: str
    intent: str                                  # what this scenario guards
    checks: list[tuple[str, dict]] = field(default_factory=list)


SCENARIOS: list[Scenario] = [
    Scenario(
        id="tiny-no-tools",
        objective="In one sentence, what is idempotency? No tools needed.",
        intent="A trivial task must stay trivial: one stage, zero containers, cents.",
        checks=[
            ("status_is", {}),
            ("max_stages", {"limit": 1}),
            ("no_tools_used", {}),
            ("max_sessions", {"limit": 2}),
            ("max_cost_usd", {"limit": 0.15}),
            ("answer_max_chars", {"limit": 600}),
        ],
    ),
    Scenario(
        id="cited-fact",
        objective="In two sentences, what is the Antikythera mechanism? Use web search and cite one source URL.",
        intent="Small tool-using task: reuse web_search, one healthy call, a real citation.",
        checks=[
            ("status_is", {}),
            ("max_stages", {"limit": 2}),
            ("tool_ok_calls", {"min_count": 1}),
            ("cites_url", {"min_count": 1}),
            ("max_cost_usd", {"limit": 0.30}),
            ("tool_error_ratio_below", {"limit": 0.5}),
        ],
    ),
    Scenario(
        id="multi-source-brief",
        objective=(
            "Survey at least 12 distinct sources on the current state of small modular "
            "nuclear reactors (SMRs) and write a ~300-word brief with source citations. "
            "Accuracy matters: verify claims against the sources before finalizing."
        ),
        intent="The scale band: planner must choose gather->map->reduce(->review), not a monolith.",
        checks=[
            ("status_is", {}),
            ("used_pattern", {"pattern": "map"}),
            ("min_workers", {"n": 2}),
            ("used_pattern", {"pattern": "reduce"}),
            ("review_ran", {}),
            ("cites_url", {"min_count": 5}),
            ("answer_min_chars", {"limit": 800}),
            ("tool_error_ratio_below", {"limit": 0.6}),
        ],
    ),
    Scenario(
        id="file-deliverable",
        objective="Write a four-line poem about containers and save it to a text file named poem.txt.",
        intent="Deliverable-producing band: a file tool is authored or reused; the file exists.",
        checks=[
            ("status_is", {}),
            ("produced_file", {"glob": "*/poem.txt"}),
            ("capability_created_or_reused", {"name_contains": "file"}),
            ("max_cost_usd", {"limit": 0.60}),
        ],
    ),
]


def get(scenario_id: str) -> Scenario:
    for s in SCENARIOS:
        if s.id == scenario_id:
            return s
    raise KeyError(f"unknown scenario '{scenario_id}' (have: {[s.id for s in SCENARIOS]})")


def validate() -> None:
    """Fail loudly on a scenario referencing a check that does not exist."""
    for s in SCENARIOS:
        for name, _params in s.checks:
            if name not in CHECKS:
                raise ValueError(f"scenario '{s.id}' references unknown check '{name}'")


validate()
