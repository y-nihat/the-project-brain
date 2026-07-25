"""The behavior scenario suite.

Generic on purpose: each scenario probes one band of the effort spectrum, so a
regression in planning judgment, tool use, fan-out, or honesty shows up as a
flipped check — regardless of topic. Edit freely; `validate()` guards against
referencing checks that do not exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

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
        intent=("Nothing about this must be OBSERVED, so the analyze step answers it: "
                "one session, no plan, no containers, cents. This check used to be "
                "unsatisfiable — the floor was planner + stage agent + supervisor — "
                "which is the debt the analyze step's full output range pays."),
        checks=[
            ("status_is", {"expected": "answered"}),
            ("max_stages", {"limit": 0}),
            ("no_tools_used", {}),
            ("max_sessions", {"limit": 1}),
            ("max_cost_usd", {"limit": 0.05}),
            ("answer_max_chars", {"limit": 800}),
        ],
    ),
    Scenario(
        id="escalation-floor",
        objective=("What is the official current stable version number of Python? "
                   "Cite the source URL you observed it on."),
        intent=("The other side of the same judgment: this cannot be answered without "
                "observing something, so the full loop must run and a real source must "
                "be cited. A cheap path that swallowed this would ship a guess as an "
                "answer. (The first attempt at this scenario asked for today's UTC date "
                "— which the agent runtime injects into its own context, so it was "
                "answerable without observing anything and the scenario measured "
                "nothing. A live trial caught it.)"),
        checks=[
            ("status_is", {}),
            ("min_stages", {"limit": 1}),
            ("cites_url", {"min_count": 1}),
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
        id="orchestration-drill",
        objective=(
            "Analyze the eighteen operations log entries below. For EACH entry produce a "
            "structured note (component; event; what it references or depends on; anomalies), "
            "dividing the per-entry analysis among parallel analysts. Then synthesize a ~250-word "
            "post-mortem with a unified UTC timeline, the root cause, and an explicit list of "
            "contradictions or gaps in the records. Work ONLY from these entries — no external "
            "research or tools are needed. Accuracy matters: verify the post-mortem against the "
            "entries before finalizing.\n\n"
            "L01 01:52 edge-lb: p99 latency rising across eu-west\n"
            "L02 01:55 monitoring: traffic snapshot captured — volume NORMAL\n"
            "L03 01:58 api-gateway: alarm storm begins; correlation notes point to the mesh RCA "
            "recorded in L15\n"
            "L04 02:01 payments-svc: timeout errors calling auth-svc spike\n"
            "L05 02:04 service-mesh: connection churn 40x baseline between all mesh services\n"
            "L06 02:09 oncall: SEV-1 declared\n"
            "L07 02:14 db-team: primary database failover INITIATED (replication lag > 90s)\n"
            "L08 02:20 platform: cache cluster restarted as a precaution\n"
            "L09 02:2— [record corrupted; remainder unrecoverable]\n"
            "L10 02:31 auth-svc: repeated mTLS handshake failures logged\n"
            "L11 02:35 service-mesh: handshake failure rate 97% on mesh sidecars\n"
            "L12 02:41 SRE handover: notes state the database failover was initiated at 02:41, "
            "AFTER the cache restart — conflicts with the db-team log\n"
            "L13 02:47 network: packet loss ruled out; all links healthy\n"
            "L14 02:55 comms: status page updated, partial outage acknowledged\n"
            "L15 03:02 RCA draft: root cause = EXPIRED TLS CERTIFICATE on the internal service "
            "mesh CA; sidecars rejecting handshakes\n"
            "L16 03:08 security: emergency certificate renewal approved\n"
            "L17 03:11 capacity review: confirms the L02 snapshot — no traffic anomaly, ruling "
            "out load as the cause\n"
            "L18 03:20 deploy: renewed certificate rolled out; error rates recovering"
        ),
        intent=("FAST coordination drill (~minutes, no tools/containers): forces cross-shard "
                "references, a contradiction, and a corrupted record — observes team signals, "
                "initiative, and honest verification."),
        checks=[
            ("status_is", {}),
            ("used_pattern", {"pattern": "map"}),
            ("min_workers", {"n": 2}),
            ("review_ran", {}),
            ("no_tools_used", {}),
            ("answer_matches", {"regex": "(contradict|conflict|discrepan|inconsisten)"}),
            ("answer_matches", {"regex": "(TLS|certificat)"}),
            ("answer_min_chars", {"limit": 600}),
            ("max_cost_usd", {"limit": 1.00}),
        ],
    ),
    Scenario(
        id="team-smoke",
        objective=(
            "Survey 6 distinct sources on the current commercialization status of "
            "solid-state batteries and write a ~150-word summary with source citations. "
            "Verify the claims against the sources before finalizing."
        ),
        intent="Fast per-slice regression: full team shape (map/reduce/review) at a fraction of the time and cost.",
        checks=[
            ("status_is", {}),
            ("used_pattern", {"pattern": "map"}),
            ("min_workers", {"n": 2}),
            ("review_ran", {}),
            ("team_activity", {"min_count": 1}),
            ("cites_url", {"min_count": 3}),
            ("answer_min_chars", {"limit": 400}),
            ("max_cost_usd", {"limit": 1.50}),
            ("tool_error_ratio_below", {"limit": 0.6}),
        ],
    ),
    Scenario(
        id="multi-source-brief",
        objective=(
            "Survey at least 12 distinct sources on the current state of small modular "
            "nuclear reactors (SMRs) and write a ~300-word brief with source citations. "
            "Accuracy matters: verify claims against the sources before finalizing."
        ),
        intent="The DEEP scale band (slow, ~$2-4): full breadth fan-out. Run sparingly; team-smoke is the per-slice loop.",
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


# An objective-NEUTRAL scenario: every check here is about how the engine
# behaved, never about what the task was, so it can be scored against ANY
# recorded run. That is what makes a cross-version baseline possible without
# spending a cent — `score_run` reads a run that already happened.
#
# The bands are deliberately generous. This is a regression tripwire, not a
# quality bar: it should stay silent through normal variation and speak up when
# something structural changes (tools failing more often, sessions ballooning,
# cost stepping up for the same work).
ENGINE_HEALTH = Scenario(
    id="engine-health",
    objective="(not run live — scored against recorded runs)",
    intent="Objective-neutral engine behaviour, scorable offline against any run.",
    checks=[
        ("tool_error_ratio_below", {"limit": 0.5}),
        ("max_sessions", {"limit": 40}),
        ("max_cost_usd", {"limit": 20.0}),
    ],
)


# Offline scenarios are NOT part of the live suite: they are scored against
# runs that already happened, so `run_suite` must never try to execute them.
OFFLINE_SCENARIOS: list[Scenario] = [ENGINE_HEALTH]


def get(scenario_id: str) -> Scenario:
    for s in SCENARIOS + OFFLINE_SCENARIOS:
        if s.id == scenario_id:
            return s
    raise KeyError(f"unknown scenario '{scenario_id}' "
                   f"(have: {[s.id for s in SCENARIOS + OFFLINE_SCENARIOS]})")


def validate(scenarios: Optional[list["Scenario"]] = None) -> None:
    """Fail loudly on a scenario the harness could not actually run.

    Two ways that happens, and only the first was checked: a check that does
    not exist, and a check that exists but does not take the parameter the
    scenario passes it. The second surfaced as a TypeError inside score_run —
    partway through a suite that costs real money — so it is bound here, at
    import, where it costs nothing.
    """
    import inspect

    from .facts import RunFacts

    for s in (SCENARIOS + OFFLINE_SCENARIOS if scenarios is None else scenarios):
        for name, params in s.checks:
            if name not in CHECKS:
                raise ValueError(f"scenario '{s.id}' references unknown check '{name}'")
            try:
                inspect.signature(CHECKS[name]).bind(RunFacts(run_id="validate"), **params)
            except TypeError as e:
                raise ValueError(
                    f"scenario '{s.id}' check '{name}' cannot be called with "
                    f"{params!r}: {e}") from None


validate()

