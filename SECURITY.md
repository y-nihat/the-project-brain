# Security policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report privately through GitHub: **[Security → Report a vulnerability](https://github.com/y-nihat/the-project-brain/security/advisories/new)**
(GitHub Private Vulnerability Reporting). That channel is monitored; a public issue
with a proof of concept is not, and it discloses the flaw to everyone at once.

If GitHub reporting is unavailable to you, open a public issue titled
"security contact request" with no details, and a private channel will be arranged.

Useful in a report: what you attacked, the exact steps to reproduce, what you got,
and the commit or tag you tested. A working proof of concept is welcome, and
speculative reports are still worth sending — say plainly what you verified and
what you inferred.

## What to expect

This project is maintained by one person. The commitments are small enough to
keep:

- **Acknowledgement within 14 days.** If you have heard nothing by then, the report
  did not arrive — please chase it.
- **A fix or a published advisory within 90 days** of acknowledgement, whichever
  the severity warrants. If a fix will take longer, you will be told why and when.
- **Coordinated disclosure.** Please give those 90 days before publishing. Credit
  in the advisory and the changelog unless you prefer otherwise.
- **No bounty.** There is no money behind this project. There is attribution, a
  fast reply, and a real fix.

## Scope

**Supported version:** the latest released tag on `main`. Older tags are not
patched; there are no backports before 1.0.

**In scope:**

- Anything that lets content the engine ingests — an objective, an attached file, a
  fetched page, a prior run's artifacts, a capability description — read, write, or
  execute outside the boundary intended for it.
- Escape from a sandbox container to the host, or from one run's workspace to
  another's.
- Any path by which the operator's credential leaves the engine: written to disk,
  reaching agent context, entering a capability container, or crossing the network.
- Defects in the containment, redaction, or approval code paths.
- A guarantee in [`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md) that the code
  does not actually enforce. Documentation that overclaims is a real finding here.

**Out of scope** — known, documented, and not defects:

- The items listed under **Known gaps** and **Non-guarantees** in
  [`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md). Read those first; a report
  restating one of them will be closed as known. A report showing one is *worse
  than documented* is in scope and welcome.
- Host compromise. Anyone with your user account or Docker daemon access can
  already read `.env`.
- A capability that is malicious by design, authored on purpose by the operator.
- Vulnerabilities in dependencies, Docker itself, or Anthropic's services — report
  those upstream.
- Anything requiring a credential you were given rather than one you obtained.

## Safe harbour

Testing against **your own installation** is authorised: it is your machine, your
Docker daemon, and your subscription. Do not test against anyone else's, do not
access data that is not yours, and do not run denial-of-service or automated
scanning against third-party services through the engine. Good-faith research
under these terms will not be pursued.

## The security model itself

What the engine guarantees, where it does not, and which gaps are known and open:
[`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md).
