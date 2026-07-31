# Security model — credentials and containment

This document states exactly where secrets can and cannot exist in this project,
and what the engine does and does not contain. To **report a vulnerability**, see
[`SECURITY.md`](../SECURITY.md) instead.

"Secrets" means the operator's `.env` credentials (e.g. `CLAUDE_CODE_OAUTH_TOKEN`)
and any credential collected at the interactive gate (API keys, bot tokens,
connection strings).

Every claim below is scoped to what a test actually proves. Where a guarantee
used to be stated more broadly than the code enforces, the narrower true version
is here and the gap is named under **Known gaps**.

## Guarantees (enforced structurally and re-proven by the test suite)

1. **Gate-collected secrets live in memory only.** The `ApprovalBroker` holds them
   for the session; they are injected into tool sandboxes engine-side as environment
   variables. They are never written to the trace DB, run folders, registry, or
   workspace — proven by byte-scan tests (`tests/test_secrets.py`,
   `tests/test_leakproof.py`) over those surfaces. The scan covers what the ENGINE
   writes; it does not cover the bytes a tool itself writes into `/work/out` (see
   Known gaps).
2. **The brain's own credential never enters a capability container.** Two paths
   had to be closed for this to be true, and the sentence used to rest on only one.
   *Environment:* a sandbox's environment is built from an explicit dict plus the
   tool's declared arguments and its one declared credential — never from
   `os.environ` (verified with a live container, not by reading). *Files:* every
   path that becomes workspace content is contained — the agent's `files` argument
   and the names a container writes (`brain.files.contained`), and the paths an
   operator names in an objective, which are now held inside the project root or the
   inbox (`resolve_file`). Before that second boundary existed, a symlink planted in
   `data/inbox` resolved to `/proc/self/environ` and carried the token into the
   workspace. Proven by `tests/test_containment.py`.
3. **Everything the tracer writes passes a redaction boundary.** Trace events,
   spans, errors, objectives, and outcomes are scrubbed
   (`brain/observability/redact.py`): values under sensitive key names and
   secret-shaped strings (Anthropic/AWS/GitHub/Slack keys, JWTs, bot tokens, PEM
   blocks) are masked before any write. Tool **stdout** is scrubbed at the sandbox
   boundary, so a buggy tool echoing its own credential cannot leak it into agent
   context, traces, or files by that route. Tool **stderr** is scrubbed at the same
   boundary, and so is every failure detail that reaches the repair prompt
   (`acquisition._error_tail`).
4. **Nothing the model names resolves outside its boundary.** The `files` argument,
   the names a container writes to `/work/out`, the `read_file` tool,
   `sample_input.files` and the paths an operator names are contained by
   `brain.files.contained`; planner stage ids are reduced to one safe path segment.
   Absolute paths, `..`, and symlinks pointing out are refused. Proven by
   `tests/test_containment.py`. **Capability names are the exception and are not
   coerced** — what rejects path syntax in a capability name today is that it must
   also be a legal Docker image reference, which the build rejects before any
   directory is created. That is a real constraint, but it is Docker's, not ours.
5. **`.env` is delivered by allowlist, not wholesale.** The compose file passes only
   the named variables the engine needs into the brain container; anything else the
   operator keeps in `.env` stays on the host. The `.env` file itself is masked inside
   the container (`/dev/null` overlay). The quickstart sets it `chmod 600` on the
   host; nothing in the engine enforces that, so it is your step, not a guarantee.
6. **Nothing secret-shaped is committed by accident.** A pre-commit hook
   (`.githooks/pre-commit`) blocks staged changes containing credential formats, and
   a suite test scans every repo file for the same shapes. This is a safety net, not
   a control: the hook is opt-in (`./scripts/setup-git.sh` installs it),
   `--no-verify` bypasses it by design, and it reads the textual diff, so a secret
   inside a file git treats as binary passes it. The full git history has been
   verified clean by an independent scan of every object.
7. **Objectives are screened.** An objective that appears to contain a credential is
   intercepted BEFORE anything is recorded; the run is refused unless explicitly
   approved, because objectives are stored permanently. Screening is shape-based and
   shares the pattern list in point 3, so it inherits that list's coverage exactly.

## Known gaps (true today, each with its fix named)

- **Tool output files land verbatim.** Bytes a tool writes to `/work/out` are copied
  into the run workspace without redaction — deliberately, since a tool's retained
  evidence is the point — so a tool that saves a credential into its own output file
  saves it in the clear.
- **The redaction pattern list is a denylist**, and therefore only as current as
  its last review. It matches today's formats from Anthropic, OpenAI, AWS, GitHub,
  GitLab, Google, Stripe, Hugging Face, npm and Slack, plus JWTs and PEM blocks
  (`tests/test_secrets.py`). The next provider's format is not matched until
  somebody adds it.
- **Sandboxes run as uid 0 inside the container**, with `cap_drop: ALL`,
  `no-new-privileges`, and the default seccomp profile.
- **A networked tool reaches more than the internet.** A capability the model marks
  `network: bridge` is placed on Docker's default bridge with a host-gateway alias,
  so it can reach the host's own services, the LAN, and link-local addresses.
- **Capability reuse is trusted on two booleans.** A promoted capability is reused
  when it is marked verified and not quarantined; it is re-probed only when the
  engine version stamp changes.
- **Model-authored Dockerfiles are built on the host daemon** with no timeout, no
  resource ceiling, as root, with network access and model-chosen unpinned packages.
- **There is no spend ceiling.** The rails are counts — agent turns, sessions per
  run, repair attempts, acquisition attempts — not dollars.
- **Prompt injection is in scope and undefended.** Content the brain ingests (a
  fetched page, an attached file, a prior run's artifacts, a capability description)
  enters agent context with no delimiting and no data-versus-instructions marker.

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
- **The pip-install path is not the contained path.** Under `pip install .` the
  orchestrator runs on your host, not in its own container. Everything above about
  the brain's own containment describes the compose topology only.

## Operator practices

- Put secrets in `.env` (600) or provide them at the gate prompt — never in
  objectives, code, tests, or docs.
- Rotate any credential you typed visibly or pasted anywhere outside the hidden prompt.
- Prefer scoped, revocable credentials for tools (e.g. a test bot's token, a
  read-only DB user).
- Bring your own subscription token; never share, pool, or commit one.
- Treat an objective you did not write, and content the brain fetches, as untrusted
  input to a system that writes and runs code.
