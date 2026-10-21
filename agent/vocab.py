"""The office's words, in one place: the roles, their skills (the kinds of work, each a kind of Claude run) and which
role does each, with each skill's place in the budget.

Used by the engine (who did a run), the office page and its API (what to call it). English, like the page; what
the roles write stays Swedish.
"""

from __future__ import annotations

ROLES = ("assistant", "director", "researcher", "manager")
NAMES = {"assistant": "Assistant", "director": "Director", "researcher": "Researcher", "manager": "Manager",
         "build": "Build mode", "owner": "you", "system": "the system", "other": "other"}

# Each kind of Claude run, the role it belongs to, and what it is called.
KINDS = {
    "chat": ("assistant", "Conversation"),
    "deliver": ("assistant", "Delivery"),
    "chat_summary": ("assistant", "Conversation summary"),
    "assistant_night": ("assistant", "Night review"),
    "director_understand": ("director", "Daily review"),
    "director_reflect": ("director", "Reflection"),
    "triage": ("director", "Triage"),
    "researcher_plan": ("researcher", "Planning"),
    "research": ("researcher", "Search mission"),
    "research_deep": ("researcher", "Wide search"),
    "page_extract": ("researcher", "Reading a watched page"),
    "scout_queries": ("researcher", "Search queries"),
    "manager": ("manager", "Daily review"),
    "dispatcher": ("manager", "Dispatcher"),
    "builder": ("build", "Code change"),
}


# Each skill's place in the budget (agent/budget.py), lowest first when room is short:
# 1 the conversation · 2 what the owner asked for · 3 the cheap pipeline that keeps the senses working ·
# 4 the roles' regular work · 5 the heavy and exploratory work. Tiers 1–3 never wait for the budget.
TIERS = {
    "chat": 1, "builder": 2,
    "deliver": 3, "chat_summary": 3, "triage": 3, "dispatcher": 3, "page_extract": 3, "scout_queries": 3,
    "assistant_night": 4, "director_understand": 4, "manager": 4,
    "researcher_plan": 5, "research": 5, "research_deep": 5, "director_reflect": 5,
}


def tier_of(kind: str) -> int:
    return TIERS.get(kind, 4)


def role_of(kind: str) -> str:
    return KINDS.get(kind, ("other", kind))[0]


def task_name(kind: str) -> str:
    return KINDS.get(kind, ("other", kind))[1]


def kinds_of(role: str) -> list[str]:
    return [k for k, (r, _) in KINDS.items() if r == role]


# Tools that go out of the office (mail, the web, GitHub): on the office page, Director borrows Researcher's
# computer for them. Names as they appear in a run's trace.
OUT_TOOLS = ("agent__mail_", "agent__web_read", "agent__github", "WebSearch")
