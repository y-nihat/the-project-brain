"""Mediated team communication.

Agents never talk to each other directly (free-form agent chat is where
inter-agent misalignment breeds — MAST); they signal the orchestrator, which
acts as the team lead:

  * ``ask_upstream``   — ask a COMPLETED earlier stage a question; the
    orchestrator runs a short, tool-less follow-up session of that stage's
    agent over its own output and returns the answer.
  * ``report_blocker`` — record an obstacle (unreachable item, missing input)
    so it lands in the trace and the blackboard instead of being silently
    worked around. A FATAL blocker is authoritative run state: the classifier
    can refuse SUCCEEDED because of it.
  * ``report_evidence_gap`` — declare that the objective depends on something
    OBSERVED that the agent's current tools cannot establish. This signal
    re-enters the core loop: the engine replans for the unmet need instead of
    letting the gap be filled from memory, prior narration, or plausibility.

One :class:`TeamChannel` exists per agent session, so the question budget is
per teammate, like a person's patience.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional

from . import config

AskFn = Callable[[str, str], Awaitable[str]]
ResearchFn = Callable[[str, str, str], Awaitable[str]]


class TeamChannel:
    def __init__(self, *, agent_id: str, completed_stages: list[str],
                 ask_fn: AskFn, blackboard=None, tracer=None,
                 max_questions: Optional[int] = None,
                 research_fn: Optional[ResearchFn] = None):
        self.agent_id = agent_id
        self.completed = list(completed_stages)
        self._ask_fn = ask_fn
        self._research_fn = research_fn
        self.blackboard = blackboard
        from .observability import NullTracer

        self.tracer = tracer or NullTracer()
        self.max_questions = max_questions if max_questions is not None else config.TEAM_MAX_QUESTIONS
        self.questions_used = 0
        self.blockers: list[dict] = []
        self.gaps: list[dict] = []

    async def research(self, topic: str, why: str = "", specifics: str = "") -> tuple[bool, str]:
        """Delegate a research request to the orchestrator (mediated — the agent
        never searches inline). The orchestrator runs a bounded research agent
        and returns a findings brief. Returns (ok, findings_or_reason). Findings
        are METHOD KNOWLEDGE / general testimony — they inform HOW to proceed,
        never source evidence about the task's designated target."""
        if self._research_fn is None:
            return False, "research delegation is not available in this session."
        topic = str(topic or "").strip()
        if len(topic) < 3:
            return False, "give a specific research topic (what you need to learn)."
        self.tracer.event("team.research", self.agent_id, status="info",
                          topic=topic[:200], why=str(why)[:200])
        findings = await self._research_fn(topic, str(why or ""), str(specifics or ""))
        return True, findings

    async def ask(self, stage: str, question: str) -> tuple[bool, str]:
        """Returns (ok, answer_or_reason)."""
        if stage not in self.completed:
            return False, (f"stage '{stage}' is not available (completed stages: "
                           f"{self.completed or 'none'})")
        if self.questions_used >= self.max_questions:
            return False, (f"question budget exhausted ({self.max_questions}); "
                           "continue with what you have.")
        self.questions_used += 1
        self.tracer.event("team.question", self.agent_id, status="info",
                          to=stage, question=question[:300], n=self.questions_used)
        answer = await self._ask_fn(stage, question)
        return True, answer

    def block(self, issue: str, needed: Optional[str] = None,
              severity: str = "recoverable") -> str:
        severity = severity if severity in ("recoverable", "fatal") else "recoverable"
        record = {"agent": self.agent_id, "issue": issue[:500],
                  "needed": (needed or "")[:300], "severity": severity}
        self.blockers.append(record)
        self.tracer.event("team.blocker", self.agent_id, status="failed",
                          issue=record["issue"], needed=record["needed"],
                          severity=severity)
        if self.blackboard is not None:
            existing = self.blackboard.get("team.blockers", []) or []
            self.blackboard.put("team.blockers", existing + [record])
        if severity == "fatal":
            return ("fatal blocker recorded — the run's final status will reflect it. "
                    "Deliver what you can, stating plainly what could not be done; "
                    "do not fabricate around it.")
        return ("blocker recorded. Continue best-effort with your remaining items; "
                "unresolved gaps will be reported honestly, do not fabricate around them.")

    def evidence_gap(self, need: str, attempted: Optional[str] = None) -> str:
        """The observation the objective depends on cannot be established with
        the agent's current tools — recorded as controller state so the engine
        can re-enter planning for the unmet need."""
        record = {"agent": self.agent_id, "need": need[:500],
                  "attempted": (attempted or "")[:300]}
        self.gaps.append(record)
        self.tracer.event("team.evidence_gap", self.agent_id, status="failed",
                          need=record["need"], attempted=record["attempted"])
        if self.blackboard is not None:
            existing = self.blackboard.get("evidence.gaps", []) or []
            self.blackboard.put("evidence.gaps", existing + [record])
        return ("evidence gap recorded — the engine will plan additional observation "
                "for it if possible. Finish your stage with what you HAVE observed, "
                "clearly marking the gap; do NOT fill it from memory, prior runs' "
                "narration, or plausibility.")

    def tool_defect(self, tool: str, error: str, tried: Optional[str]) -> str:
        """A tool the agent HOLDS is broken (crashes, wrong output, cannot
        express what the task needs) — recorded as controller state so the
        ENGINE can repair the capability and re-run the stage. Closes the
        dead end where an agent declared 'inherent tool limitation', shipped a
        degraded result, and the repair machinery — which exists — never
        heard about the defect."""
        record = {"agent": self.agent_id, "tool": str(tool)[:120],
                  "error": str(error)[:600], "tried": (tried or "")[:400]}
        self.tracer.event("team.tool_defect", self.agent_id, status="failed",
                          tool=record["tool"], error=record["error"][:200])
        if self.blackboard is not None:
            existing = self.blackboard.get("tool.defects", []) or []
            self.blackboard.put("tool.defects", existing + [record])
        return ("tool defect recorded — the engine will attempt a repair and re-run "
                "this stage with the fixed tool. Finish this attempt economically: "
                "deliver what you can with the tools that work, state plainly what "
                "the defective tool blocked, and do NOT paper over the gap with a "
                "quality-degrading workaround unless the repair fails too.")


TEAM_TOOLS = ["mcp__team__ask_upstream", "mcp__team__report_blocker",
              "mcp__team__report_evidence_gap", "mcp__team__progress_update",
              "mcp__team__request_research", "mcp__team__report_tool_defect"]


def build_team_server(channel: TeamChannel):
    """Expose the channel to one agent session as MCP tools."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool("ask_upstream",
          "Ask a teammate that already finished an earlier stage a specific question "
          "about their work (e.g. an unclear item, a missing URL). Use sparingly.",
          {"type": "object",
           "properties": {"stage": {"type": "string", "description": "completed stage id"},
                          "question": {"type": "string"}},
           "required": ["stage", "question"]})
    async def ask_upstream(args):
        ok, text = await channel.ask(str(args.get("stage", "")), str(args.get("question", "")))
        return {"content": [{"type": "text", "text": text}], **({} if ok else {"is_error": True})}

    @tool("report_blocker",
          "Report an obstacle you cannot resolve (unreachable source, missing input, "
          "impossible item) instead of silently working around it. severity='fatal' when "
          "the objective itself cannot be met because of it — that becomes the run's "
          "final status; 'recoverable' (default) when your stage can still deliver value.",
          {"type": "object",
           "properties": {"issue": {"type": "string"},
                          "needed": {"type": "string", "description": "what would unblock you"},
                          "severity": {"type": "string", "enum": ["recoverable", "fatal"]}},
           "required": ["issue"]})
    async def report_blocker(args):
        text = channel.block(str(args.get("issue", "")), args.get("needed"),
                             str(args.get("severity", "recoverable")))
        return {"content": [{"type": "text", "text": text}]}

    @tool("report_evidence_gap",
          "The objective depends on something OBSERVED (content, events, structure, state) "
          "that your tools cannot establish — e.g. the required channel is absent or your "
          "observation covers identity but not the substance asked about. Report the gap so "
          "the engine can plan a different observation method. NEVER fill such a gap from "
          "memory, prior answers, or plausibility.",
          {"type": "object",
           "properties": {"need": {"type": "string",
                                   "description": "what must be observed, stated abstractly"},
                          "attempted": {"type": "string",
                                        "description": "what you tried and what it yielded"}},
           "required": ["need"]})
    async def report_evidence_gap(args):
        text = channel.evidence_gap(str(args.get("need", "")), args.get("attempted"))
        return {"content": [{"type": "text", "text": text}]}

    @tool("progress_update",
          "Announce the ACTION you are doing right now, in one short line (e.g. 'drafting the "
          "parser', 'writing corrected file; next: artifact inspection'). Report the current "
          "action, NOT a conclusion you have not verified (never 'all defects fixed' before "
          "the check runs). Call at milestones during long work.",
          {"type": "object", "properties": {"status": {"type": "string"}},
           "required": ["status"]})
    async def progress_update(args):
        note = str(args.get("status", ""))[:200]
        channel.tracer.event("agent.progress", channel.agent_id, status="info", note=note)
        return {"content": [{"type": "text", "text": "noted — continue."}]}

    @tool("request_research",
          "When you recognize you LACK the KNOWLEDGE to proceed — an unfamiliar library or "
          "API, an opaque error you don't understand, a technique or design direction you "
          "need to learn — ask the orchestrator to research it. A research agent investigates "
          "and returns a findings brief. Do NOT search inline yourself; delegate here. "
          "Findings are METHOD KNOWLEDGE (how to proceed) and general web testimony — use them "
          "to choose your approach; they are NOT source evidence about the task's designated "
          "target and must never be presented as observations of it.",
          {"type": "object",
           "properties": {"topic": {"type": "string",
                                    "description": "the specific thing to learn/investigate"},
                          "why": {"type": "string",
                                  "description": "why you need it (what you're stuck on)"},
                          "specifics": {"type": "string",
                                        "description": "exact error text, versions, constraints"}},
           "required": ["topic"]})
    async def request_research(args):
        ok, text = await channel.research(str(args.get("topic", "")),
                                          str(args.get("why", "")),
                                          str(args.get("specifics", "")))
        return {"content": [{"type": "text", "text": text}], **({} if ok else {"is_error": True})}

    @tool("report_tool_defect",
          "One of YOUR tools is defective — it crashes, returns the wrong shape, or "
          "cannot express something the task's quality bar requires (a missing "
          "parameter/feature). Report it instead of accepting the limitation: the "
          "engine can REPAIR tools and will re-run this stage with the fixed tool. "
          "Do not use this for missing evidence (report_evidence_gap) or external "
          "obstacles (report_blocker).",
          {"type": "object",
           "properties": {"tool": {"type": "string",
                                   "description": "the defective tool's name"},
                          "error": {"type": "string",
                                    "description": "exact error text, or the concrete "
                                                   "capability it lacks"},
                          "tried": {"type": "string",
                                    "description": "what you tried before reporting"}},
           "required": ["tool", "error"]})
    async def report_tool_defect(args):
        text = channel.tool_defect(str(args.get("tool", "")), str(args.get("error", "")),
                                   args.get("tried"))
        return {"content": [{"type": "text", "text": text}]}

    return create_sdk_mcp_server(name="team", version="1.0.0",
                                 tools=[ask_upstream, report_blocker,
                                        report_evidence_gap, progress_update,
                                        request_research, report_tool_defect])
