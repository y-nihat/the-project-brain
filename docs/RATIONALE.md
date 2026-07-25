# Why each subsystem exists — a map into the CHANGELOG

`CHANGELOG.md` is 88 KB and is the design record for most of this engine: nearly
every entry is a post-mortem naming the live run that failed and the general
rule that failure earned. That makes it the most valuable document in the
repository and the hardest to use — it is indexed by version number, so
answering *"why does the approval gate void a passing review?"* means reading
it end to end.

This page is the index it lacks. Each row names a subsystem, the file that
implements it, and the releases that shaped it. Read the entry, not just the
diff: the entry says what broke.

> **Bus factor.** One author, ~300 commits. If a second developer ever touches
> this codebase, this page is where they should start after `CONTRIBUTING.md`.

---

## The invariant everything serves

`CONTRIBUTING.md` states it and `docs/GENERALITY_AUDIT.md` audits it: the core
loop is *analyze → decompose → identify requirements → generate agents/tools →
execute*, statable with **no task, topic, or tool name**; capabilities are
identified by **contract**, never by name; there are **no fixed pipelines**.
Every change below had to be expressible without naming the task that provoked
it.

---

## Planning and control

| Subsystem | Implementation | Shaped by |
| --- | --- | --- |
| Plan schema, target signal model, evidence tiers | `brain/planner.py` | 0.11.0, 0.17.0, 0.20.0 |
| Proxy-first accountability gate + corrective replan | `brain/planner.py`, `orchestrator._correct_proxy_first_plan` | 0.17.0, 0.20.0 |
| Stage graph execution (single/map/reduce/review) | `brain/workflow.py`, `orchestrator._run_*` | 0.11.0, 0.12.0 |
| Terminal-state classifier — one authoritative state machine | `orchestrator._classify` | 0.17.0, 0.20.0 |
| Adaptive recovery: typed method failures, replanning | `orchestrator._recover_failed_stage` | 0.13.0, 0.15.0, 0.19.0 |
| Persistence judgment (keep_trying / stop_and_ask / stop) | `brain/supervisor.py` | 0.19.0, 0.25.0 |
| Information epochs — new information earns new attempts | `brain/acquisition.py`, `brain/supervisor.py` | 0.25.0 |
| Standing goals and the outcome loop | `brain/goals.py`, `brain/memory/goals.py` | 0.27.0 |

## Evidence, review, and the honesty floor

| Subsystem | Implementation | Shaped by |
| --- | --- | --- |
| Lineage ledger, evidence tiers, fixpoint propagation | `brain/evidence.py` | 0.14.0, 0.18.0 |
| Observation states (`observed_absent` vs `not_detected`) | `brain/evidence.py`, developer contract | 0.16.0, 0.20.0 |
| Deterministic approval-void gate (claims table) | `orchestrator._claims_gate` | 0.18.0, 0.20.0 |
| Corpus manifest — citable evidence for absence claims | `orchestrator._write_corpus_manifest` | 0.15.0 |
| Count-criteria measurement, scoped bounds | `brain/criteria.py` | 0.17.0, 0.22.0, 0.28.0 |
| Claim strength must match evidence strength | reviewer protocol, `orchestrator._run_review` | 0.20.0 |
| Review round survival, courier fetches | `orchestrator._run_review` | 0.15.0, 0.18.0, 0.19.1 |
| The answer states why a run did not certify | `orchestrator._trust_report` | 0.28.0 |

## The capability plane

| Subsystem | Implementation | Shaped by |
| --- | --- | --- |
| Contract-driven authoring loop | `brain/acquisition.py` | 0.11.0, 0.13.0 |
| Generic evidence-contract verification | `acquisition._generic_verify` | 0.14.0, 0.15.0 (V060) |
| Fabrication probes (no-input, irrelevant-input) | `acquisition._fabrication_probes` | 0.15.0, 0.21.0, 0.28.0 |
| Atomic promotion (candidate tag → retag) | `acquisition._author_capability` | 0.13.1 |
| Validation epochs, revalidation, quarantine | `acquisition.validation_epoch`, `revalidate_capability` | 0.19.0, 0.21.0, 0.23.2 |
| Runtime repair of degraded tools | `acquisition.repair_capability` | 0.19.0 |
| Binary-vs-text input nature | developer contract | 0.23.1 |
| Reuse by key, then by contract | `brain/resolver.py` | 0.28.0 |
| Catalog hygiene, task-residue stripping | `registry.qualified_catalog`, `acquisition.generalize_description` | 0.21.0, 0.28.0 |
| Innate needs declared as data | `brain/bootstrap.py` | 0.26.0 |

## Safety substrate

| Subsystem | Implementation | Shaped by |
| --- | --- | --- |
| Sandbox policy: no network, ro rootfs, dropped caps | `brain/sandbox/policy.py` | 0.1.0 |
| Container ledger, leak detection, janitor | `brain/sandbox/manager.py`, `brain/ledger.py` | 0.1.0, 0.9.0 |
| Locked agent sessions (`tools=[]`, deny-by-default) | `brain/factory.py`, `brain/approval.py` | 0.1.0, 0.8.0 |
| Approval gate, hidden credential collection | `brain/approval.py` | 0.8.0, 0.24.0 |
| Operator-provisioned credentials for unattended runs | `approval.preseed_from_env` | 0.24.0 |
| Credential usage contract + re-verification | `brain/acquisition.py` | 0.24.2, 0.27.0 |
| Dry-run verification for world-acting tools | `acquisition._generic_verify`, `Capability.side_effects` | 0.27.0 |
| Redaction boundary | `brain/observability/redact.py` | 0.8.0, 0.28.0 |
| Host-published service doorway | `sandbox/policy.py` (`extra_hosts`) | 0.23.0 |

## Memory and observability

| Subsystem | Implementation | Shaped by |
| --- | --- | --- |
| Blackboard (per-run controller state) | `brain/memory/blackboard.py` | 0.10.0 |
| Cross-run continuity and workspace seeding | `brain/memory/history.py` | 0.14.0, 0.18.0, 0.28.0 |
| Independent-trial mode | `brain/session.py` | 0.24.4 |
| Tracer, run rollups, trace.md | `brain/observability/tracer.py` | 0.3.0, 0.28.0 |
| Live event stream | `brain/observability/stream.py` | 0.12.0, 0.28.0 |
| OTLP / Phoenix export | `brain/observability/otel_export.py` | 0.12.0 |
| Corpus statistics and offline scoring | `observability/view.py`, `brain/evals/runner.py` | 0.28.0 |
| Eval harness | `brain/evals/` | 0.11.0, 0.28.0 |

---

## Incidents worth reading in full

Six entries explain more of the system's shape than any other documentation:

- **0.15.0** — round survival and evidence provisioning. Why a review that runs
  out of rounds does not simply ship, and why the engine provisions evidence
  the reviewer demands.
- **0.17.0** — review convergence and measurement truth. Why a measured count
  outranks a reviewer's verdict.
- **0.20.0** — *the project's first false success.* Claim strength must match
  evidence strength; this is the entry that most directly explains the
  honesty floor.
- **0.22.0** — scoped measurement. A false-violation generator with authority,
  and how a fully compliant deliverable was measured non-compliant.
- **0.25.0** — epoch-aware persistence. Why repetition only counts *within* an
  information epoch.
- **0.26.0** — the generality audit. The standing rule every later change is
  tested against.

## Known gaps

`docs/ARCHITECTURE.md` §11 tracks the deliberately-open ones (a cost governor,
uncertainty-driven active observation, observe-once consolidation). `CHANGELOG.md`
carries a "Known limitations" block for redaction coverage, persistent services,
cross-session credential storage, and workspace-mount mode.
