"""The office's words, in one place: the roles, the kinds of work and which role does each.

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


def role_of(kind: str) -> str:
    return KINDS.get(kind, ("other", kind))[0]


def task_name(kind: str) -> str:
    return KINDS.get(kind, ("other", kind))[1]


def kinds_of(role: str) -> list[str]:
    return [k for k, (r, _) in KINDS.items() if r == role]
