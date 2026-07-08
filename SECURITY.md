# Security model — credentials and containment

This document states exactly where secrets can and cannot exist in this project.
"Secrets" means the operator's `.env` credentials (e.g. `CLAUDE_CODE_OAUTH_TOKEN`)
and any credential collected at the interactive gate (API keys, bot tokens,
connection strings).

## Guarantees (enforced structurally and re-proven by the test suite)

1. **Gate-collected secrets live in memory only.** The `ApprovalBroker` holds them
   for the session; they are injected into tool sandboxes engine-side as environment
   variables. They are never written to the trace DB, run folders, registry, or
   workspace — proven by byte-scan tests (`tests/test_secrets.py`, `tests/test_leakproof.py`).
2. **Everything written to disk passes a redaction boundary.** All trace events,
   spans, errors, objectives, and outcomes are scrubbed (`brain/observability/redact.py`):
   values under sensitive key names and secret-shaped strings (Anthropic/AWS/GitHub/
   Slack keys, JWTs, bot tokens, PEM blocks) are masked before any write. Tool stdout
   is scrubbed at the sandbox boundary, so a buggy tool echoing its own credential
   cannot leak it into agent context, traces, or files.
3. **`.env` is delivered by allowlist, not wholesale.** The compose file passes only
   the named variables the engine needs into the brain container; anything else the
   operator keeps in `.env` stays on the host. The `.env` file itself is masked inside
   the container (`/dev/null` overlay) and is `chmod 600` on the host.
4. **Sandboxes see only what a tool declares.** Capability containers receive an
   explicit per-call environment (tool arguments + that tool's declared credential),
   never the brain's environment.
5. **Nothing secret-shaped can be committed.** A pre-commit hook (`.githooks/pre-commit`)
   blocks staged changes containing credential formats; a suite test scans every repo
   file for the same shapes. The full git history has been verified clean.
6. **Objectives are screened.** An objective that appears to contain a credential is
   intercepted BEFORE anything is recorded; the run is refused unless explicitly
   approved, because objectives are stored permanently. Secrets belong in the hidden
   gate prompt, which the brain raises when a tool needs them.

## Non-guarantees (know the boundary)

- **Your terminal scrollback.** If you choose visible typing (`show`) at the secret
  prompt, the value appears on screen and in your terminal's history. The masked
  receipt (default) avoids this.
- **The value is real while in use.** Tools receive the actual credential at run
  time inside their sandbox; a malicious-by-design tool could transmit it to the
  service it calls (that is its job) or elsewhere if given network access. Review
  the authored tool code under `data/capabilities/<name>/build/` for high-value
  credentials, and prefer scoped/revocable keys.
- **Redaction is pattern-based.** Unusual credential formats not matching known
  shapes and not stored under sensitive key names could evade masking. Extend
  `redact.py` patterns for formats you use.
- **Host compromise is out of scope.** Anyone with access to your user account or
  Docker daemon can read `.env` and container environments.

## Operator practices

- Put secrets in `.env` (600) or provide them at the gate prompt — never in
  objectives, code, tests, or docs.
- Rotate any credential you typed visibly or pasted anywhere outside the hidden prompt.
- Prefer scoped, revocable credentials for tools (e.g. a test bot's token, a
  read-only DB user).
