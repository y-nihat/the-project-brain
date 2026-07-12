"""The evidence ledger: deterministic, typed accounting of what a run has
ACTUALLY retained, at what epistemic tier.

The V060 failure this module closes: a run's "evidence plane" existed only as
prompt assembly. A reviewer could be told (correctly) that zero evidence was
retained, run 17 deterministic lookups that all returned NOT FOUND, and still
emit 'approved' — and the engine accepted the parsed verdict over its own
facts. Narrated evidence ("sources were fetched and confirmed") outranked the
empty workspace because nothing ever computed the difference.

The ledger is that computation. It is built from two facts the engine already
owns — the files tools retained in the workspace, and the provenance each tool
REPORTED at call time (source_refs / coverage, appended by the capability
runtime to ``evidence.jsonl`` beside the workspace) — and classifies every
entry by tier:

  * ``target``  — a retained payload from accessing a DESIGNATED source (a
    locator named in the objective, or a user-attached input file);
  * ``proxy``   — a retained payload from some other source (search indexes,
    third-party pages): real evidence, but testimony ABOUT the target, never
    observation OF it;
  * ``retained``— a file with no recorded provenance (legacy tools): evidence
    of unknown lineage.

Text an agent produced is NOT in the ledger, whatever the agent calls it.
Everything here is deterministic and side-effect-free.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# A remote locator names user-referenced evidence, exactly like an attached
# file — format-neutral (any scheme://host or www.host), never a service or
# task pattern. (Shared with the orchestrator's review trigger.)
EXTERNAL_LOCATOR = re.compile(r"\b\w{2,10}://\S+|\bwww\.\S+\.\S+", re.IGNORECASE)

_LOG_NAME = "evidence.jsonl"


@dataclass
class EvidenceEntry:
    file: str            # workspace-relative path of the retained payload
    capability: str      # tool that retained it ("input" for user attachments)
    tier: str            # "target" | "proxy" | "retained"
    source_refs: list    # locators the tool reported ACTUALLY accessing
    coverage: str        # the tool's reported yield for the requested property


def designated_locators(objective: str) -> list[str]:
    """The sources the OBJECTIVE itself designates (remote locators named in
    the user's words). User-attached files are designated by construction."""
    return [m.group(0).rstrip('.,;:)"”’') for m in
            EXTERNAL_LOCATOR.finditer(objective or "")]


def record(run_dir: Path, capability: str, files: list[str],
           source_refs: list, coverage: str) -> None:
    """Append one tool call's retention facts (called by the capability
    runtime — the only writer). Best-effort: provenance must never fail a
    tool call."""
    try:
        line = json.dumps({"cap": capability, "files": list(files),
                           "source_refs": [str(s)[:300] for s in (source_refs or [])][:12],
                           "coverage": str(coverage or "")[:20]})
        with (Path(run_dir) / _LOG_NAME).open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _provenance(run_dir: Path) -> dict[str, dict]:
    """Latest recorded provenance per retained file (workspace-relative)."""
    out: dict[str, dict] = {}
    log = Path(run_dir) / _LOG_NAME
    if not log.is_file():
        return out
    for line in log.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        for f in rec.get("files") or []:
            out[str(f)] = rec
    return out


def _matches_target(refs: list, locators: list[str]) -> bool:
    """Did the tool access a designated source? Deterministic and honest about
    its precision: a ref matches when it contains a designated locator (or the
    locator contains the ref) — redirects and mirrors are NOT resolved; an
    unmatched genuine access degrades to 'proxy', never the other way."""
    for ref in refs or []:
        r = str(ref).lower()
        for loc in locators:
            l = loc.lower()
            if l in r or (len(r) > 12 and r in l):
                return True
    return False


def ledger(workspace: Path, reader_dirs: tuple, locators: list[str],
           run_dir: Optional[Path] = None) -> list[EvidenceEntry]:
    """Compute the run's retained-evidence ledger.

    ``reader_dirs`` — capability names whose output role is observation (the
    engine's existing evidence surface). When it is empty (no registry, or no
    observation-role capability exists), every tool directory counts: an empty
    surface must degrade toward showing MORE files to the gate, never toward
    silently passing an empty plane.
    """
    root = Path(workspace)
    if not root.is_dir():
        return []
    run_dir = Path(run_dir) if run_dir is not None else root.parent
    prov = _provenance(run_dir)
    entries: list[EvidenceEntry] = []

    inp = root / "input"
    if inp.is_dir():
        for p in sorted(inp.rglob("*")):
            if p.is_file():
                entries.append(EvidenceEntry(
                    file=str(p.relative_to(root)), capability="input",
                    tier="target", source_refs=[], coverage=""))

    dirs = [d for d in sorted(root.iterdir())
            if d.is_dir() and d.name not in ("input", "prior")]
    if reader_dirs:
        dirs = [d for d in dirs if d.name in reader_dirs]
    for d in dirs:
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(root))
            rec = prov.get(rel)
            if rec is None:
                tier, refs, cov = "retained", [], ""
            else:
                refs = rec.get("source_refs") or []
                cov = str(rec.get("coverage") or "")
                tier = "target" if _matches_target(refs, locators) else "proxy"
            entries.append(EvidenceEntry(file=rel, capability=d.name,
                                         tier=tier, source_refs=refs, coverage=cov))
    return entries


def describe(entries: list[EvidenceEntry], locators: list[str]) -> str:
    """Human/LLM-facing ledger summary for review context — states the tiers
    plainly so 'a well-organized report about the sources' can never read as
    'the sources'."""
    if not entries:
        return ("RETAINED EVIDENCE LEDGER: EMPTY. No tool retained any payload "
                "this run. Agent-written text is narration, not evidence.")
    lines = [f"RETAINED EVIDENCE LEDGER ({len(entries)} file(s); tiers: 'target' = "
             "payload from a DESIGNATED source"
             + (f" {locators[:3]}" if locators else "")
             + "; 'proxy' = payload from other sources — testimony ABOUT the "
               "target, not observation OF it; 'retained' = unknown lineage):"]
    for e in entries[:24]:
        refs = f" refs={e.source_refs[:2]}" if e.source_refs else ""
        cov = f" coverage={e.coverage}" if e.coverage else ""
        lines.append(f"- [{e.tier}] {e.file} (via {e.capability}{refs}{cov})")
    if len(entries) > 24:
        lines.append(f"- …{len(entries) - 24} more")
    if not any(e.tier == "target" for e in entries):
        lines.append("NOTE: no target-tier evidence exists — every retained payload "
                     "is proxy or unknown lineage; claims about the designated "
                     "source rest on testimony and must carry that limitation.")
    return "\n".join(lines)
