"""The evidence ledger: deterministic, typed accounting of what a run has
ACTUALLY retained, at what epistemic tier — with lineage that flows THROUGH
transformations.

Two observed failure classes shaped this module. First (V060): a run's
"evidence plane" existed only as prompt assembly — a reviewer approved on
narration with zero retained payloads, and the engine took the verdict over its
own facts. Second (V0100): a run derived its answer from genuine
property-preserving transformations of the designated target, but the formal
ledger excluded them because their producing tools declared a different output
role — the success was real, the certificate hollow. The general principle:

  **Evidence-ness is determined by LINEAGE (what a payload was derived from),
  not by the producing tool's self-declared role.**

The capability runtime records, per tool call: the files retained, the remote
locators ACTUALLY accessed, the workspace files ACTUALLY consumed, the reported
yield, and a content hash per file. The ledger then propagates lineage:

  * ``target``   — a payload from direct access of a DESIGNATED source (a
    locator named in the objective, or a user-attached input file);
  * ``derived``  — produced by a transformation that consumed target/derived
    payloads (frames from a recording, pages from a document, rows from a
    database dump, output of a compiled program under test): target-lineage;
  * ``proxy``    — a payload from other sources, or derived only from such
    payloads (search indexes, third-party pages): testimony ABOUT the target;
  * ``retained`` — no recorded provenance: unknown lineage. Visible, but
    unknown lineage is never proof.

Text an agent produced is NOT in the ledger, whatever the agent calls it.
Everything here is deterministic and side-effect-free (record() appends only).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# A remote locator names user-referenced evidence, exactly like an attached
# file — format-neutral (any scheme://host or www.host), never a service or
# task pattern. (Shared with the orchestrator's review trigger.)
EXTERNAL_LOCATOR = re.compile(r"\b\w{2,10}://\S+|\bwww\.\S+\.\S+", re.IGNORECASE)

_LOG_NAME = "evidence.jsonl"

# Standardized observation outcomes (recorded when tools report them):
# observed_present / observed_absent / not_detected / ambiguous /
# observation_failed. Only `observed_absent` can ever support a claim that the
# target LACKS a property; not_detected and observation_failed cannot.
OBSERVATION_STATES = ("observed_present", "observed_absent", "not_detected",
                      "ambiguous", "observation_failed")

# Tiers that constitute TARGET LINEAGE — evidence of the designated source
# itself (directly fetched or property-preserving derived), as opposed to
# testimony about it.
TARGET_TIERS = ("target", "derived")


@dataclass
class EvidenceEntry:
    file: str            # workspace-relative path of the retained payload
    capability: str      # tool that retained it ("input" for user attachments)
    tier: str            # "target" | "derived" | "proxy" | "retained"
    source_refs: list    # locators the tool reported ACTUALLY accessing
    coverage: str        # the tool's reported yield for the requested property
    state: str = ""      # standardized observation outcome, when reported
    role: str = ""       # the producing capability's declared output role
    inputs: list = field(default_factory=list)  # workspace files it consumed
    sha256: str = ""     # content hash (identity/audit)


def designated_locators(objective: str) -> list[str]:
    """The sources the OBJECTIVE itself designates (remote locators named in
    the user's words). User-attached files are designated by construction."""
    return [m.group(0).rstrip('.,;:)"”’') for m in
            EXTERNAL_LOCATOR.finditer(objective or "")]


def _hash_file(path: Path) -> str:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return ""


def record(run_dir: Path, capability: str, files: list[str],
           source_refs: list, coverage: str,
           inputs: Optional[list] = None, state: str = "",
           role: str = "", workspace: Optional[Path] = None) -> None:
    """Append one tool call's retention facts (called by the capability
    runtime — the only writer). ``inputs`` are the workspace-relative files the
    call consumed — the edges lineage propagates along. Best-effort: provenance
    must never fail a tool call."""
    try:
        hashes = {}
        if workspace is not None:
            for f in files:
                digest = _hash_file(Path(workspace) / str(f))
                if digest:
                    hashes[str(f)] = digest
        line = json.dumps({
            "cap": capability,
            "files": [str(f) for f in files],
            "source_refs": [str(s)[:300] for s in (source_refs or [])][:12],
            "coverage": str(coverage or "")[:20],
            "inputs": [str(i) for i in (inputs or [])][:24],
            "state": str(state or "")[:24],
            "role": str(role or "")[:16],
            "sha256": hashes,
        })
        with (Path(run_dir) / _LOG_NAME).open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _records(run_dir: Path) -> list[dict]:
    log = Path(run_dir) / _LOG_NAME
    if not log.is_file():
        return []
    out = []
    for line in log.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
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


def ledger(workspace: Path, reader_dirs: tuple = (), locators: Optional[list] = None,
           run_dir: Optional[Path] = None) -> list[EvidenceEntry]:
    """Compute the run's retained-evidence ledger with LINEAGE PROPAGATION.

    Every file a tool retained appears (tier states its trust); user inputs are
    target by construction. Lineage flows through transformations: a file
    produced by a call that consumed target-lineage inputs is ``derived``
    (target lineage), whatever the producing tool's declared role — a
    self-declared role can under- or over-state, lineage cannot. Consuming only
    proxy inputs yields proxy. ``reader_dirs`` is accepted for compatibility
    but no longer restricts the listing.
    """
    root = Path(workspace)
    if not root.is_dir():
        return []
    locators = list(locators or [])
    run_dir = Path(run_dir) if run_dir is not None else root.parent
    records = _records(run_dir)

    # Lineage fixpoint over recorded calls: seed target-lineage with user
    # inputs and direct designated-source fetches, then propagate through
    # consumed->produced edges until stable (handles out-of-order records).
    lineage: dict[str, str] = {}    # path -> "target" | "derived" | "proxy"
    inp = root / "input"
    if inp.is_dir():
        for p in sorted(inp.rglob("*")):
            if p.is_file():
                lineage[str(p.relative_to(root))] = "target"
    # INHERITED lineage: records seeded from a prior run carry each file's
    # tier as computed IN THAT RUN (its locators are not this run's, so the
    # tier cannot be recomputed here). Without this, a revision run's sources
    # arrive provenance-naked and the run can never certify traceability to
    # the original materials (observed: target_evidence: 0 on a revision).
    for rec in records:
        for f, t in (rec.get("tiers") or {}).items():
            if t in ("target", "derived", "proxy"):
                lineage.setdefault(str(f), t)

    changed = True
    while changed:
        changed = False
        for rec in records:
            files = [str(f) for f in rec.get("files") or []]
            if not files:
                continue
            refs = rec.get("source_refs") or []
            inputs = [str(i) for i in rec.get("inputs") or []]
            if _matches_target(refs, locators):
                new = "target"
            elif any(lineage.get(i) in TARGET_TIERS for i in inputs):
                new = "derived"
            elif refs or any(lineage.get(i) == "proxy" for i in inputs):
                new = "proxy"
            else:
                continue    # no lineage information for this call
            rank = {"proxy": 0, "derived": 1, "target": 2}
            for f in files:
                if rank.get(new, 0) > rank.get(lineage.get(f, ""), -1):
                    lineage[f] = new
                    changed = True

    prov: dict[str, dict] = {}
    for rec in records:
        for f in rec.get("files") or []:
            prov[str(f)] = rec

    entries: list[EvidenceEntry] = []
    if inp.is_dir():
        for p in sorted(inp.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(root))
                entries.append(EvidenceEntry(
                    file=rel, capability="input", tier="target",
                    source_refs=[], coverage="", state="", role="",
                    sha256=_hash_file(p)))

    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name in ("input", "prior"):
            continue
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(root))
            rec = prov.get(rel)
            if rec is None:
                entries.append(EvidenceEntry(file=rel, capability=d.name,
                                             tier="retained", source_refs=[],
                                             coverage=""))
                continue
            entries.append(EvidenceEntry(
                file=rel, capability=d.name,
                tier=lineage.get(rel, "retained"),
                source_refs=rec.get("source_refs") or [],
                coverage=str(rec.get("coverage") or ""),
                state=str(rec.get("state") or ""),
                role=str(rec.get("role") or ""),
                inputs=[str(i) for i in rec.get("inputs") or []],
                sha256=str((rec.get("sha256") or {}).get(rel, ""))))
    # Inherited seeds WITH provenance are ledger entries too (a revision run's
    # evidence base); unrecorded prior files stay invisible — seeds are not
    # evidence unless their lineage came with them.
    prior_dir = root / "prior"
    if prior_dir.is_dir():
        for p in sorted(prior_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(root))
            rec = prov.get(rel)
            if rec is None:
                continue
            entries.append(EvidenceEntry(
                file=rel, capability=str(rec.get("cap") or "prior"),
                tier=lineage.get(rel, "retained"),
                source_refs=rec.get("source_refs") or [],
                coverage=str(rec.get("coverage") or ""),
                state=str(rec.get("state") or ""),
                role=str(rec.get("role") or ""),
                inputs=[str(i) for i in rec.get("inputs") or []],
                sha256=str((rec.get("sha256") or {}).get(rel, ""))))
    return entries


def inherit_records(run_dir: Path, seed_dir: Path, seed_id: str) -> int:
    """Carry a seeded prior run's PROVENANCE forward: its evidence records are
    translated to this run's ``prior/<seed_id>/…`` paths, each file stamped
    with the tier it had in the prior run (computed with the PRIOR run's
    locators — this run cannot re-derive them). The prior run's user inputs
    inherit ``target`` by construction. Best-effort like all provenance."""
    seed_ws = Path(seed_dir) / "workspace"
    try:
        locs = json.loads((Path(seed_dir) / "locators.json").read_text())
    except (OSError, ValueError):
        locs = []
    seed_entries = ledger(seed_ws, (), [str(x) for x in locs or []],
                          run_dir=Path(seed_dir))
    tier_of = {e.file: e.tier for e in seed_entries}
    count = 0
    try:
        with (Path(run_dir) / _LOG_NAME).open("a") as fh:
            for rec in _records(Path(seed_dir)):
                files = [f"prior/{seed_id}/{f}" for f in rec.get("files") or []]
                if not files:
                    continue
                tiers = {f"prior/{seed_id}/{f}": tier_of.get(str(f), "retained")
                         for f in rec.get("files") or []}
                fh.write(json.dumps({
                    "cap": str(rec.get("cap") or ""),
                    "files": files,
                    "source_refs": rec.get("source_refs") or [],
                    "coverage": str(rec.get("coverage") or ""),
                    "inputs": [f"prior/{seed_id}/{i}" for i in rec.get("inputs") or []],
                    "state": str(rec.get("state") or ""),
                    "role": str(rec.get("role") or ""),
                    "sha256": {f"prior/{seed_id}/{k}": v
                               for k, v in (rec.get("sha256") or {}).items()},
                    "tiers": tiers,
                    "inherited_from": seed_id,
                }) + "\n")
                count += 1
            inp = seed_ws / "input"
            if inp.is_dir():
                in_files = [f"prior/{seed_id}/{p.relative_to(seed_ws)}"
                            for p in sorted(inp.rglob("*")) if p.is_file()]
                if in_files:
                    fh.write(json.dumps({
                        "cap": "input", "files": in_files, "source_refs": [],
                        "coverage": "", "inputs": [], "state": "", "role": "",
                        "sha256": {},
                        "tiers": {f: "target" for f in in_files},
                        "inherited_from": seed_id,
                    }) + "\n")
                    count += 1
    except OSError:
        return count
    return count


def target_lineage(entries: list[EvidenceEntry]) -> list[EvidenceEntry]:
    """The entries that constitute evidence OF the designated source itself."""
    return [e for e in entries if e.tier in TARGET_TIERS]


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
             + "; 'derived' = property-preserving transformation of target "
               "payloads (TARGET LINEAGE — evidence of the source itself); "
               "'proxy' = payload from other sources — testimony ABOUT the "
               "target, not observation OF it; 'retained' = unknown lineage, "
               "never proof):"]
    for e in entries[:40]:
        bits = []
        if e.source_refs:
            bits.append(f"refs={e.source_refs[:2]}")
        if e.inputs:
            bits.append(f"from={e.inputs[:2]}")
        if e.coverage:
            bits.append(f"coverage={e.coverage}")
        if e.state:
            bits.append(f"state={e.state}")
        lines.append(f"- [{e.tier}] {e.file} (via {e.capability}"
                     + ("; " + " ".join(bits) if bits else "") + ")")
    if len(entries) > 40:
        lines.append(f"- …{len(entries) - 40} more")
    if not target_lineage(entries):
        lines.append("NOTE: no target-lineage evidence exists — every retained payload "
                     "is proxy or unknown lineage; claims about the designated "
                     "source rest on testimony and must carry that limitation.")
    return "\n".join(lines)
