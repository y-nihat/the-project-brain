"""Rendering trace records as a live progress stream.

Pure and I/O-free: one trace record in, one display line (or None) out. It
lives here rather than in the REPL because it renders TRACE data, not terminal
state — both entry points stream the same run the same way, and the one-shot
CLI must not have to import the interactive one to do it.
"""
from __future__ import annotations

from typing import Optional


# Compact glyphs for the live stream.
_ICON = {"ok": "✓", "succeeded": "✓", "granted": "✓", "passed": "✓",
         "failed": "✗", "denied": "⛔", "running": "…", "info": "·"}


def format_event(record: dict) -> Optional[str]:
    """Render one trace record as a compact live-progress line, or None to skip.

    Pure and unit-tested — the REPL just prints whatever this returns.
    """
    kind = record.get("kind")
    name = record.get("name") or ""
    status = record.get("status") or "info"
    data = record.get("data") or {}
    phase = record.get("phase")
    dur = record.get("duration_ms")
    dur_s = f" ({dur/1000:.1f}s)" if isinstance(dur, (int, float)) and dur else ""

    # Spans emit a start and an end; render only the informative side per kind.
    if kind == "run":
        return None  # the REPL frames the run itself
    if kind == "plan.created":
        stages = data.get("stages") or data.get("agents") or []
        shape = ", ".join(f"{s.get('id', '?')}({s.get('pattern', 'single')})" for s in stages)
        return f"  📋 plan: {len(stages)} stage(s) — {shape}"
    if kind == "stage":
        if phase == "start":
            return f"  ▶ stage {name} [{data.get('pattern', 'single')}]"
        line = f"  ■ stage {name} done{dur_s}"
        head = (data.get("output_head") or "").strip()
        if head:
            body = "\n".join(f"  │ {ln}" for ln in head.splitlines()[:14])
            line += f"\n{body}\n  └─ (full output: outputs/{name}.md)"
        return line
    if kind == "run.classification":
        cls = data.get("classification")
        icon = "✅" if cls == "succeeded" else "⚠"
        return f"  {icon} result: {str(cls).upper()} — {str(data.get('reason', ''))[:90]}"
    if kind == "plan.criteria":
        crit = data.get("criteria") or []
        return f"  ✓ success criteria ({len(crit)}): " + " · ".join(str(c)[:50] for c in crit[:4])
    if kind == "map.fanout":
        return (f"     ⑃ fan-out: {data.get('items')} items → {data.get('workers')} workers "
                f"(≤{data.get('shard_size')} items each)")
    if kind in ("map.no_manifest", "map.empty"):
        return f"     ⚠ {name}: {'no machine-readable manifest upstream' if kind == 'map.no_manifest' else 'no work items'}"
    if kind == "worker.failed":
        return f"     ✗ worker {name} failed: {str(data.get('error', ''))[:80]}"
    if kind == "reduce.batched":
        return f"     ⑂ consolidating hierarchically in {data.get('batches')} batches"
    if kind == "review.verdict":
        v = data.get("verdict")
        icon = "✓" if v == "approved" else "✎"
        return f"     {icon} review round {data.get('round')}: {v}"
    if kind == "review.revised":
        return f"     ↻ draft revised after review ({name})"
    if kind == "review.evidence" and phase == "start":
        return "     ⇣ reviewer demanded missing evidence — courier fetching…"
    if kind == "review.failed":
        return f"     ⚠ reviewer died ({str(data.get('error', ''))[:60]}) — draft delivered UNVERIFIED"
    if kind == "team.question":
        return f"     🗨 {name} asks {data.get('to')}: {str(data.get('question', ''))[:70]}"
    if kind == "agent.progress":
        return f"     ⋯ {name}: {str(data.get('note', ''))[:90]}"
    if kind == "artifact.repair":
        return f"     🔧 mechanical repair (attempt {data.get('attempt')}): {len(data.get('issues') or [])} defect(s) → {name}"
    if kind == "team.blocker":
        return f"     ✋ {name} reports a blocker: {str(data.get('issue', ''))[:70]}"
    if kind == "agent.salvaged":
        return f"     ♻ {name} ran out of budget — partial findings salvaged ({data.get('chars')} chars)"
    if kind == "map.retry":
        return f"     ↻ retrying failed shard(s) {data.get('shards')} with fresh sessions"
    if kind == "stage.failed":
        return f"  ⚠ stage {name} failed ({str(data.get('error', ''))[:70]}) — delivering best effort"
    if kind == "review.exhausted":
        return f"     ⚠ review rounds exhausted — answer carries an honesty note"
    if kind == "run.session_cap":
        return f"  ⚠ session cap reached — {name} not run"
    if kind == "agent.created":
        caps = data.get("capabilities") or []
        cap_s = f" [{', '.join(caps)}]" if caps else " [no tools — reasons unaided]"
        return f"  🤖 agent ▸ {data.get('role', name)}{cap_s}"
    if kind == "tool.result":
        r = data.get("result")
        n = data.get("count")
        if r == "ok":
            return f"         → ok ({n} result{'s' if n != 1 else ''})"
        return f"         → {r}{' — tool may be degraded' if r == 'empty' else ''}"
    if kind == "capability.unhealthy":
        return f"     ⚠ {name} looks unhealthy ({str(data.get('reason', ''))[:80]}) — repairing…"
    if kind == "agent.rerun":
        return f"     ↻ re-running {name} with the repaired tool"
    if kind == "decision":
        if data.get("choice") == "reuse":
            return f"     ↩ reuse tool: {data.get('capability')}"
        if data.get("choice") == "author":
            return f"     ✎ needs a new tool: {data.get('capability')} — authoring…"
        return None
    if kind == "agent.session" and phase == "start" and name.startswith("developer:"):
        return f"       developer writing {name.split(':',1)[1]}…"
    if kind == "capability.build" and phase == "start":
        return f"       build ▸ {name} (attempt {data.get('attempt', '?')}, {data.get('source','?')})"
    if kind == "capability.verify" and phase != "start":
        ok = data.get("passed")
        tail = f" — {data.get('detail','')}" if not ok else ""
        return f"       verify {_ICON['passed'] if ok else _ICON['failed']} {name}{dur_s}{tail[:100]}"
    if kind == "capability.promote" and phase != "start":
        return f"       promoted {name} ✓ (now reusable)"
    if kind == "sandbox.run" and phase != "start":
        oom = " OOM!" if data.get("oom") else ""
        return f"       ▸ ran {name} exit={data.get('exit_code')}{oom}{dur_s}"
    if kind == "approval.requested":
        return f"  ⚠ approval needed: {data.get('action')}"
    if kind == "approval.resolved":
        return f"  ⚠ approval {status}"
    if kind == "supervisor.consult":
        if data.get("action") == "proceed":
            return None  # silent when nothing needs doing
        return f"  🧭 supervisor [{name}]: {data.get('action')} — {str(data.get('reason', ''))[:80]}"
    if kind == "supervisor.redo":
        return f"  🧭 supervisor sent {name} back: {str(data.get('guidance', ''))[:80]}"
    if kind == "supervisor.continuation":
        verb = "another attempt" if data.get("action") == "continue" else "STOP authoring"
        return f"     ⚖ continuation judgment: {verb} — {str(data.get('reason', ''))[:80]}"
    if kind == "acquire.epoch":
        return f"     ↳ new information (epoch {data.get('epoch')}): {str(data.get('note', ''))[:90]}"
    if kind == "goal.declared":
        return f"  🎯 standing goal: {str(data.get('metric', ''))[:90]}"
    if kind == "goal.verdict":
        return (f"  🎯 goal outcome [{name}]: {str(data.get('verdict', '')).upper()}"
                + (f" (measured: {data.get('value')})" if data.get("value") else ""))
    if kind == "goal.pursuit":
        return (f"  🎯 goal pursuit [{name}] iteration {data.get('iteration')}/"
                f"{data.get('budget')}: {data.get('action')} — "
                f"{str(data.get('reason', ''))[:70]}")
    if kind == "secret.invalidated":
        return f"  🔑 cached {name} invalidated: {str(data.get('reason', ''))[:70]}"
    if kind == "supervisor.error":
        return None  # fail-open is silent; the trace has it
    if kind == "user.informed":
        return None  # the broker already printed the message itself
    if kind == "files.attached":
        return f"  📎 files in workspace: {', '.join(data.get('files') or [])}"
    if kind == "secret.requested":
        return f"  🔑 secret needed: {name} — {str(data.get('purpose', ''))[:70]}"
    if kind == "secret.provided":
        return (f"  🔑 {name} provided (held in memory for this session only)"
                if status == "granted" else f"  🔑 {name} NOT provided")
    if kind == "capability.revalidate" and phase == "start":
        return f"     🧪 revalidating remembered tool {name} (stamped: {data.get('stamped')})"
    if kind == "capability.quarantined":
        return f"     ☣ {name} QUARANTINED: {str(data.get('reason', ''))[:80]} — re-authoring"
    if kind == "capability.unavailable":
        return f"     ⛔ tool {name} unavailable this session (needs {data.get('needs')})"
    if kind == "agent.tool_call":
        short = name.split("__")[-1]
        prev = data.get("args_preview")
        return f"       ⚒ {short}({prev})" if prev else f"       ⚒ {short}"
    return None
