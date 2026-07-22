"""Goal-level judgment: the outcome loop that closes a run against its aspiration.

Three pieces, mirroring the persistence/continuation judgments proven at the
tool-authoring level, lifted to the OBJECTIVE level:

* ``judge_outcome`` — after a run ends, compare what was actually achieved
  against the standing goal: extract the realized measured value (never a
  self-estimate), give a verdict, a diagnosis, and a concrete next step. A
  REGRESSION mandates a research-first next step — a failed attempt means the
  current model of what works is wrong.
* ``next_iteration_decision`` — inside an authorized pursuit budget, decide
  whether another self-initiated iteration is justified. Fails CLOSED to
  stop: a judgment failure must never spawn autonomous runs (the opposite
  polarity from in-run judgments, which fail open because ceilings bound them).
* ``compose_next_objective`` — deterministic assembly of the next iteration's
  objective from the goal state and the decision (pure; testable).

Safety posture: pursuit only ever runs under a USER-GRANTED budget extracted
from the objective text (the planner is barred from inventing one); every
iteration is a full normal run — approval gates and credential prompts apply
unchanged; hard ceilings (GOAL_PURSUIT_MAX_ATTEMPTS, the budget itself) bound
the loop regardless of judgment.
"""
from __future__ import annotations

from typing import Optional

from . import config

VERDICTS = ("achieved", "advanced", "unchanged", "regressed", "unknown")

_OUTCOME_SYSTEM = (
    "You are the outcome judge of an autonomous run that pursued a MEASURABLE goal. "
    "You see the goal (metric, direction, target, how the true value is observed), the "
    "attempt history, and the run's final answer. Decide:\n"
    "- value: the REALIZED measured value this run obtained, exactly as observed from the "
    "world (an external evaluator's response, a real measurement). NEVER accept an internal "
    "estimate (a cross-validation figure, a projection, a self-assessment) as the value — "
    "if only estimates exist, leave value empty and verdict=unknown, and make next_step "
    "'obtain the true measurement' concrete.\n"
    "- verdict: achieved (target met), advanced (better than the best prior attempt), "
    "unchanged (no better), regressed (worse than the best prior attempt), unknown.\n"
    "- diagnosis: ONE short paragraph — why this outcome, judged from the answer's evidence.\n"
    "- next_step: the single most valuable next action toward the goal. HARD RULE: after a "
    "REGRESSION the next step MUST begin with researching/diagnosing why the approach "
    "failed before building anything — a regression means the current model of what works "
    "is wrong, and rebuilding on it repeats the failure.\n"
    "Be exact about numbers; never invent a value the answer does not state. Call "
    "emit_decision exactly once."
)

_OUTCOME_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": "string", "description": "realized measured value, or empty"},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "diagnosis": {"type": "string"},
        "next_step": {"type": "string"},
    },
    "required": ["verdict", "diagnosis", "next_step"],
}

_PURSUE_SYSTEM = (
    "You govern an AUTHORIZED, budget-bounded pursuit of a measurable goal: the user "
    "granted a budget of self-initiated iterations, and you decide after each attempt "
    "whether the next one is justified. You see the goal, the full attempt trajectory "
    "(values, verdicts, diagnoses), and the remaining budget. Decide:\n"
    "- continue: a materially different, promising next approach exists (state it in "
    "next_approach — it becomes the next run's brief). Do not continue to repeat an "
    "approach category that already failed; after a regression the next iteration must "
    "begin with research into why.\n"
    "- stop: the trajectory has plateaued (attempts are no longer learning), the target "
    "is reached, or remaining budget is better returned to the user with a recommendation. "
    "State what you would tell the user.\n"
    "Spending the budget is not the goal — advancing the metric is. Call emit_decision "
    "exactly once."
)

_PURSUE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["continue", "stop"]},
        "next_approach": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["action", "reason"],
}


def _attempt_lines(goal: dict, attempts: list[dict]) -> list[str]:
    lines = [f"GOAL #{goal['id']}: {goal['metric']} [direction: {goal['direction']}"
             + (f", target: {goal['target']}" if goal.get("target") else "")
             + "]"
             + (f"\nMEASURED BY: {goal['measured_by']}" if goal.get("measured_by") else ""),
             f"ATTEMPTS SO FAR ({len(attempts)}):"]
    for i, a in enumerate(attempts, 1):
        lines.append(f"  {i}. run {a['run_id']}: value={a['value'] or '(none)'} "
                     f"verdict={a['verdict']}"
                     + (f" — {a['diagnosis'][:200]}" if a.get("diagnosis") else ""))
    if not attempts:
        lines.append("  (none)")
    return lines


async def _emit(system_prompt: str, schema: dict, prompt: str, label: str,
                model=None) -> dict:
    """One locked, single-tool judgment session (the supervisor's pattern)."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    from .factory import locked_session
    from .observability import trace_query

    captured: dict = {}

    @tool("emit_decision", "Emit your decision as structured data.", schema)
    async def emit_decision(args):
        captured.update(args or {})
        return {"content": [{"type": "text", "text": "decision recorded"}]}

    server = create_sdk_mcp_server(name="goal", version="1.0.0", tools=[emit_decision])
    options = locked_session(
        system_prompt=system_prompt,
        mcp_servers={"goal": server},
        allowed_tools=["mcp__goal__emit_decision"],
        model=model or config.model_for("goal"),
        max_turns=3,
    )
    from .observability import NullTracer

    await trace_query(NullTracer(), label, prompt, options)
    return captured


async def judge_outcome(goal: dict, attempts: list[dict], answer: str, *,
                        best: Optional[str] = None, model=None,
                        decide_fn=None) -> dict:
    """Outcome verdict for a finished run. Fail-open to verdict=unknown —
    an outcome-judgment failure must never break the run's delivery."""
    digest = "\n".join(
        _attempt_lines(goal, attempts)
        + [f"BEST MEASURED VALUE SO FAR: {best or '(none)'}",
           "", "THE JUST-FINISHED RUN'S FINAL ANSWER:", "---",
           (answer or "")[:8000], "---",
           "Judge the outcome now."])
    try:
        raw = await (decide_fn(digest) if decide_fn
                     else _emit(_OUTCOME_SYSTEM, _OUTCOME_SCHEMA, digest, "goal.outcome",
                                model=model))
        raw = raw if isinstance(raw, dict) else {}
    except Exception as e:  # noqa: BLE001 — outcome judgment must never break delivery
        return {"value": "", "verdict": "unknown",
                "diagnosis": f"(outcome judgment failed: {type(e).__name__})",
                "next_step": "obtain the true measurement of the goal metric"}
    verdict = str(raw.get("verdict") or "unknown").strip().lower()
    return {"value": str(raw.get("value") or "")[:100],
            "verdict": verdict if verdict in VERDICTS else "unknown",
            "diagnosis": str(raw.get("diagnosis") or "")[:1500],
            "next_step": str(raw.get("next_step") or "")[:1500]}


async def next_iteration_decision(goal: dict, attempts: list[dict], *,
                                  attempts_left: int, best: Optional[str] = None,
                                  model=None, decide_fn=None) -> dict:
    """Continue/stop inside an authorized pursuit budget. Fails CLOSED to stop."""
    digest = "\n".join(
        _attempt_lines(goal, attempts)
        + [f"BEST MEASURED VALUE SO FAR: {best or '(none)'}",
           f"REMAINING AUTHORIZED ITERATIONS: {attempts_left}",
           "Decide: continue (state next_approach) / stop (state what to tell the user)."])
    try:
        raw = await (decide_fn(digest) if decide_fn
                     else _emit(_PURSUE_SYSTEM, _PURSUE_SCHEMA, digest, "goal.pursuit",
                                model=model))
        raw = raw if isinstance(raw, dict) else {}
    except Exception as e:  # noqa: BLE001 — a judgment failure must not spawn runs
        return {"action": "stop", "next_approach": "",
                "reason": f"fail-closed: pursuit judgment failed ({type(e).__name__})"}
    action = str(raw.get("action") or "stop").strip().lower()
    return {"action": action if action in ("continue", "stop") else "stop",
            "next_approach": str(raw.get("next_approach") or "")[:2000],
            "reason": str(raw.get("reason") or "")[:300]}


def compose_next_objective(goal: dict, last_verdict: str, best: Optional[str],
                           next_approach: str) -> str:
    """The next self-initiated iteration's objective (pure, deterministic).
    A regression FORCES a research-first instruction into the brief."""
    parts = [
        f"[standing goal #{goal['id']}] Advance the goal: {goal['metric']} "
        f"({goal['direction']}"
        + (f", target {goal['target']}" if goal.get("target") else "") + ").",
        f"Best measured value so far: {best or 'none yet'}; the previous attempt's "
        f"outcome verdict was {last_verdict.upper()}.",
    ]
    if last_verdict == "regressed":
        parts.append(
            "Begin with a RESEARCH stage into why the previous approach performed "
            "worse before building anything — the prior model of what works is "
            "contradicted by the measured outcome.")
    if next_approach.strip():
        parts.append("Approach for this attempt: " + next_approach.strip())
    parts.append(
        "Obtain the TRUE measured value of the metric for the new attempt (the same "
        "way it was measured before) and report it against the best so far.")
    return "\n".join(parts)


def goal_banner(goal: dict, outcome: dict, best: Optional[str],
                attempts_count: int, pursuing: bool) -> str:
    """The user-facing goal section appended to a run's answer."""
    lines = [
        "",
        "---",
        f"[standing goal #{goal['id']}] {goal['metric']} — verdict: "
        f"{outcome['verdict'].upper()}"
        + (f" (measured: {outcome['value']})" if outcome.get("value") else "")
        + (f"; best across {attempts_count} attempt(s): {best}" if best else ""),
    ]
    if outcome.get("diagnosis"):
        lines.append(f"Diagnosis: {outcome['diagnosis']}")
    if outcome["verdict"] not in ("achieved",) and outcome.get("next_step"):
        lines.append(f"Proposed next step: {outcome['next_step']}")
        if not pursuing:
            lines.append(
                "To continue: state it in your next objective, or grant an iteration "
                "budget in the objective (e.g. 'pursue this goal, up to N attempts').")
    return "\n".join(lines)
