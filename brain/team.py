"""Mediated team communication.

Agents never talk to each other directly (free-form agent chat is where
inter-agent misalignment breeds — MAST); they signal the orchestrator, which
acts as the team lead:

  * ``ask_upstream``   — ask a COMPLETED earlier stage a question; the
    orchestrator runs a short, tool-less follow-up session of that stage's
    agent over its own output and returns the answer.
  * ``report_blocker`` — record an obstacle (unreachable item, missing input)
    so it lands in the trace and the blackboard instead of being silently
    worked around; the agent is told to continue best-effort.

One :class:`TeamChannel` exists per agent session, so the question budget is
per teammate, like a person's patience.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional

from . import config

AskFn = Callable[[str, str], Awaitable[str]]


class TeamChannel:
    def __init__(self, *, agent_id: str, completed_stages: list[str],
                 ask_fn: AskFn, blackboard=None, tracer=None,
                 max_questions: Optional[int] = None):
        self.agent_id = agent_id
        self.completed = list(completed_stages)
        self._ask_fn = ask_fn
        self.blackboard = blackboard
        from .observability import NullTracer

        self.tracer = tracer or NullTracer()
        self.max_questions = max_questions if max_questions is not None else config.TEAM_MAX_QUESTIONS
        self.questions_used = 0
        self.blockers: list[dict] = []

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

    def block(self, issue: str, needed: Optional[str] = None) -> str:
        record = {"agent": self.agent_id, "issue": issue[:500], "needed": (needed or "")[:300]}
        self.blockers.append(record)
        self.tracer.event("team.blocker", self.agent_id, status="failed",
                          issue=record["issue"], needed=record["needed"])
        if self.blackboard is not None:
            existing = self.blackboard.get("team.blockers", [])
            self.blackboard.put("team.blockers", existing + [record])
        return ("blocker recorded. Continue best-effort with your remaining items; "
                "unresolved gaps will be reported honestly, do not fabricate around them.")


TEAM_TOOLS = ["mcp__team__ask_upstream", "mcp__team__report_blocker",
              "mcp__team__progress_update"]


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
          "impossible item) instead of silently working around it.",
          {"type": "object",
           "properties": {"issue": {"type": "string"},
                          "needed": {"type": "string", "description": "what would unblock you"}},
           "required": ["issue"]})
    async def report_blocker(args):
        text = channel.block(str(args.get("issue", "")), args.get("needed"))
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

    return create_sdk_mcp_server(name="team", version="1.0.0",
                                 tools=[ask_upstream, report_blocker, progress_update])
