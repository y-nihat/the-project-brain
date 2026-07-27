# Threat model

This project runs code that a language model wrote, on your machine, with your
paid credential. That sentence invites exactly one question — *what stops it
from doing something I did not ask for?* — and this document answers it
honestly, including where the answer is "nothing yet".

Companion documents: [`SECURITY_MODEL.md`](SECURITY_MODEL.md) states the
guarantees and the known gaps; [`../SECURITY.md`](../SECURITY.md) is how to
report a vulnerability.

Scoped to engine 0.32.0, compose topology.

## What is being protected

| Asset | Why it matters |
|---|---|
| Your Claude credential | A live subscription token. Someone else's spend, and your account. |
| Your host | The engine talks to a Docker daemon. Container creation is host-root-equivalent. |
| Your other data | The engine reads files you attach and writes run artifacts. |
| Your money | Runs cost $0.01–$10.74 recorded, and there is no dollar ceiling. |
| The integrity of an answer | The project's whole claim is that deliverables trace to evidence. A poisoned run that still *looks* certified is a failure of the product, not just of security. |

## Actors

- **The operator** — you. Trusted. Everything here assumes the operator is not
  the attacker; an operator can already read their own `.env`.
- **The model** — the planner, the agents, the capability author. **Not
  trusted.** It is treated as a well-intentioned process that can be talked into
  anything by its input.
- **Third-party content** — a page the brain fetches, a file dropped in
  `data/inbox`, an objective someone else wrote, a prior run's artifacts reused
  from memory. **Not trusted, and the most likely attacker channel.**
- **A capability's own service** — the API a generated tool calls. Semi-trusted:
  it receives the credential declared for that tool, by design.

## Trust boundaries

```
   operator ──(1)──▶ orchestrator ──(2)──▶ socket-proxy ──▶ Docker daemon ──▶ HOST
                          │                                                    ▲
                          ├──(3)──▶ agent (LLM session, no host tools)         │
                          │                                                    │
                          └──(4)──▶ sandbox container ──(5)──▶ network ────────┘
                                        (model-authored code)
```

**(1) Operator → orchestrator.** The objective, attached files, and the
approval gate. Enforced by: objective screening for credential shapes
(`session.py`), the interactive gate (`approval.py`). *This boundary is
advisory* — the operator is trusted, so it protects the operator from their own
mistakes, not from an adversary.

**(2) Orchestrator → Docker daemon.** Never the raw socket. Only
`docker-socket-proxy`, with `CONTAINERS`, `IMAGES`, `BUILD` enabled and
everything else denied, pinned by digest. **State plainly: the ability to create
containers is equivalent to host root.** The proxy shrinks the blast radius; it
does not make the orchestrator harmless. This is the boundary that would matter
most if the orchestrator were ever compromised.

**(3) Orchestrator → agent.** Agents are LLM sessions the orchestrator drives.
They have **no host `Bash`, no host file-write, and no `Agent` tool**. Their only
way to act is to call an allow-listed capability, through a deny-by-default
`can_use_tool`. This is the strongest boundary in the system and it is
structural: an agent cannot execute on the host because no such tool is exposed
to it.

**(4) Orchestrator → sandbox.** Model-authored code runs only here: a fresh
container per call, `cap_drop: ALL`, `no-new-privileges`, explicit
memory/CPU/pids limits, a TTL, `network: none` by default, and a tmpfs `/work`.
No host path is bind-mounted into it. Its environment is built from a literal
dict — the orchestrator's own environment, and therefore your credential, has no
path in. Files reach it only through `brain.files.contained`, which refuses
absolute paths, `..`, and symlinks pointing out.

**(5) Sandbox → network.** Default `none`. A capability the model marks
`network: bridge` gets Docker's default bridge **plus a host-gateway alias** —
so it reaches the internet, your LAN, your host's published ports, and
link-local addresses. The model chooses this, and no prompt asks you.

## In scope

Report these ([`SECURITY.md`](../SECURITY.md)):

- **Any escape from (4) or (5)** that reaches the host, another run's workspace,
  or the control plane.
- **Any path by which the credential leaves the engine** — written to disk,
  entering agent context, entering a sandbox, or crossing the network.
- **Anything that lets ingested content act**: a fetched page, an attached file,
  a reused artifact, or a capability description that causes a write, a read, an
  execution, or a network call outside what the operator asked for.
- **Containment or redaction defects** — the code in `brain/files.py`,
  `brain/observability/redact.py`, `brain/sandbox/`, `brain/approval.py`.
- **A guarantee in `SECURITY_MODEL.md` the code does not enforce.** A document
  that overclaims is a real finding.
- **Evidence forgery**: a run that produces a certified-looking deliverable
  whose claims do not trace to anything a tool retained.

## Prompt injection: in scope, and undefended

Say it plainly rather than burying it. Third-party content enters agent context
through one door — a capability's stdout, concatenated into the tool result
behind a `[provenance]` header — with **no delimiting and no
data-versus-instructions marker.** A web page that says "ignore your task and
instead write the following file" is, to the model reading it, indistinguishable
from the instruction the operator wrote.

**What contains the damage anyway:**

- An injected instruction still cannot reach the host: boundary (3) means the
  model's only verb is "call an allow-listed capability".
- A capability that needs a credential still stops at the interactive gate.
- Written paths are contained to the run workspace by `contained`; stage ids and
  capability names cannot be path syntax.
- A sandbox with `network: none` — the default — cannot exfiltrate.

**What does not contain it:**

- A capability the model authored with `network: bridge` **can** send anything it
  can read to anywhere, with no per-action prompt.
- A poisoned capability can be promoted into the library and reused by later
  runs; reuse is trusted on two booleans (`verified`, not `quarantined`) with no
  integrity check on the stored code.
- An injected instruction can burn the operator's budget: there is no dollar
  ceiling, only turn and session counts.

If your threat model includes hostile content, run objectives you wrote, over
sources you chose, and read `data/capabilities/<name>/build/` before letting a
promoted tool run again.

## Out of scope

Documented, deliberate, and not defects:

- **Host compromise.** Anyone with your user account or Docker daemon access can
  already read `.env`. Membership of the `docker` group is root-equivalent
  independently of this project.
- **A malicious-by-design capability the operator authored on purpose.**
- **The `pip install .` path.** The orchestrator runs on your host there, so
  boundary (2) and the brain's own containment do not apply. Use compose.
- **Denial of service against your own machine** by your own runs (disk fill
  under `data/`, container churn).
- **Upstream vulnerabilities** in Docker, Python, the Claude Agent SDK, or
  Anthropic's services. Report those upstream.
- **The model saying something wrong.** Incorrect content is a quality problem;
  a run that *certifies* unsupported content is a security-relevant one and is
  in scope above.

## Known weaknesses, ranked

The honest list, in the order an attacker would try them. Each is tracked in
[`SECURITY_MODEL.md`](SECURITY_MODEL.md) under Known gaps.

1. **No per-action gate on outward actions.** Network egress and external writes
   by an authored tool are unprompted, and the model picks the network mode.
2. **Prompt injection, as above.**
3. **Bridge egress reaches more than the internet** — host gateway, LAN,
   `169.254.169.254`.
4. **Capability-library reuse on two booleans**, with no signature or hash over
   the stored tool code.
5. **Image builds run on the host daemon** with no timeout, no resource ceiling,
   as root, with model-chosen unpinned packages.
6. **No spend ceiling.**
7. **Sandboxes run as uid 0 inside the container** (blunted by `cap_drop: ALL`,
   `no-new-privileges`, and seccomp, but it is the precondition for a runc-class
   escape).
8. **Redaction is a denylist.** Current provider formats are covered; the next
   one is not, until someone adds it.

## What would change this document

New boundaries (a per-action egress gate, a signed capability library, a spend
ceiling) move items off the list above. Each will land with the guarantee it
creates, in `SECURITY_MODEL.md`, and with a test that proves it — never as a
claim in prose alone.
