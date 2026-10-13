"""Every way the engine calls Claude, in one place (the map behind `audax claude`).

Each ClaudeTask kind used in the code must be registered here; tests/test_calls.py enforces it, so the
map can't drift from what actually runs. All calls go through ClaudeGate to Claude Code in the
workspace, on the owner's subscription. Nothing here runs on Claude's own scheduled services.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Call:
    role: str  # Assistant | Director | Researcher | Manager | Senses | Builder | Tooling (docs/MODEL.md)
    purpose: str
    prompt: str  # prompt file(s) or where the instruction lives
    model: str  # settings.models.<key>, or a literal
    tools: str
    trigger: str


CALLS: dict[str, Call] = {
    "chat": Call("Assistant", "The conversation, and writing every proactive message (internal turns)",
                 "prompts/chat.md + claude_md.md + style/state/interests", "models.chat",
                 "memory files, web search, web_read, github, mail (read), control/inbox tools", "each message from the owner; each delivery"),
    "triage": Call("Director", "Value features and a factual brief per new event, in batches",
                   "prompts/triage.md", "models.triage", "none (structured output)", "new events from senses or research"),
    "dispatcher": Call("Manager", "Whether, when and how pending items reach the owner",
                       "prompts/dispatcher.md", "models.dispatcher", "none (structured output)", "every 10 min when the outbox changed"),
    "research": Call("Researcher", "Run a mission's search queries and return checked findings",
                     "prompts/research.md", "models.research", "web search, web_read", "per mission interval (1-4 days)"),
    "research_deep": Call("Researcher", "A wide sweep of a mission (about 20 queries), when planning asks for one",
                          "prompts/research.md (deep sweep)", "models.research", "web search, web_read",
                          "when Researcher's planning asks for one, daytime, 3 h apart"),
    "researcher_plan": Call("Researcher", "Plan the search: maps, portfolios, leads, coverage, watched sources, missions",
                            "prompts/researcher.md", "models.research", "web search, web_read, memory (read)",
                            "weekly; or when Director orders, a portfolio runs thin, or a mission goes quiet"),
    "page_extract": Call("Senses", "Pull new items out of a watched page's changed lines",
                         "inline in sources/pages.py", "models.triage", "none (structured output)", "a watched page changed"),
    "director_understand": Call("Director", "Keep the picture of the owner current; steer missions and senses",
                                "prompts/director.md + director_understand.md", "models.director", "memory (read), web search, web_read, mail (read)",
                                "nightly 04:15; when woken from the chat"),
    "director_reflect": Call("Director", "Deep reflection: hidden assets, gaps, connections; checked ideas",
                             "prompts/director.md + director_reflect.md", "models.reflect (high effort)", "web search, web_read, github, mail (read), memory (read)",
                             "Mon/Wed/Sat 05:00"),
    "manager": Call("Manager", "UX review of the conversation; pacing, style, state, shelf",
                    "prompts/manager.md", "models.manager", "memory (read); its own files are written from its answer", "nightly 03:30; when woken from the chat"),
    "builder": Call("Builder", "Change the engine on a branch in a sandboxed worktree",
                    "prompts/builder.md", "models.builder", "files in the worktree, read-only git", "the owner asks for a change in chat"),
    "smoketest": Call("Tooling", "Check that Claude Code answers", "inline in smoketest.py", "models.triage", "none", "by hand"),
    "debug": Call("Tooling", "One-shot question via /claude", "the message itself", "models.chat", "none", "the /claude command"),
}
