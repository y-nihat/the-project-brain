"""Evaluation harness: repeatable behavior scenarios for the whole brain.

Unit tests prove the engine's mechanics deterministically; this harness scores
the brain's *behavior* on live objectives — the part that cannot be asserted
exactly. Each scenario pairs an objective with deterministic checks applied to
the run's recorded facts (trace DB + data-layer files), so after a major engine
change you re-run the suite and diff the scorecards.

  python -m brain.evals list
  python -m brain.evals run [scenario ...]     # live: runs objectives ($, minutes)
  python -m brain.evals score <run_id> <scenario>   # re-score an existing run
"""
from .facts import RunFacts, load_facts
from .runner import run_suite, score_run
from .scenarios import SCENARIOS

__all__ = ["RunFacts", "load_facts", "run_suite", "score_run", "SCENARIOS"]
