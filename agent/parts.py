"""A prompt in three parts (instance/granskning/2026-11-12-omgang-16-kunskapen.md, "Modellen").

Every role's and skill's prompt answers three questions, each with its owner:

- Det vi vet: what Audax knows within the skill's areas. About the owner: their own words first, the office's
  interpretations marked. About the work: the lessons of the role (practice beliefs). Written by what the owner says
  and by the roles' conclusions; the skill's areas are set here, by what the skill does, the same for every owner.
- Det jag följer: what the role chose to keep an eye on, items and saved questions (agent/threads.py). The role's.
- Att avgöra nu: the points that need an answer, each with its basis (agent/docket.py). The engine's.

Everything else is fetched with the tools. Each part has a budget; what doesn't fit isn't dropped in silence, it
becomes a line saying how much more there is and how to fetch it.

Stage 1 builds the parts and measures them; the roles move to them one at a time (Researcher first).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from agent import docket, threads
from agent.beliefs import current, source_note
from agent.db import Database
from agent.forms import AREAS
from agent.items import ref

# The areas of the owner each skill works within. None: all of them (the skills whose work is the owner as a whole).
SKILL_AREAS: dict[str, tuple[str, ...] | None] = {
    "triage": ("interests", "opportunities", "not_interested", "school", "limits", "compensate"),
    "research": ("interests", "opportunities", "not_interested", "school", "limits"),
    "research_deep": ("interests", "opportunities", "not_interested", "school", "limits"),
    "researcher_plan": ("interests", "opportunities", "not_interested", "school", "limits", "compensate", "who"),
    "check": ("interests", "opportunities", "not_interested"),
    "deliver": ("communication", "name", "limits", "interrupt", "who"),
    "dispatcher": ("schedule", "interrupt", "communication", "limits"),
    "manager": ("communication", "schedule", "interrupt", "limits", "compensate"),
    "chat": None,
    "assistant_night": None,
    "director_understand": None,
    "director_reflect": None,
}
SKILL_ROLE = {"triage": "director", "research": "researcher", "research_deep": "researcher", "researcher_plan": "researcher",
              "check": "researcher", "deliver": "assistant", "dispatcher": "manager", "manager": "manager", "chat": "assistant",
              "assistant_night": "assistant", "director_understand": "director", "director_reflect": "director"}
BUDGET = {"known": 6000, "following": 5000, "decide": docket.BUDGET}


async def known(db: Database, skill: str, owner_name: str = "ägaren", tz: str = "Europe/Stockholm", budget: int = BUDGET["known"]) -> str:
    """Det vi vet: the owner within the skill's areas (their words first), then the role's lessons."""
    today = datetime.now(ZoneInfo(tz)).date()
    areas = SKILL_AREAS.get(skill, None)
    beliefs = [b for b in await current(db, today=today) if b["kind"] != "practice" and b.get("setting") is None]
    if areas is not None:
        beliefs = [b for b in beliefs if (b.get("area") or "other") in areas]
    order = {a: i for i, a in enumerate(AREAS)}
    beliefs.sort(key=lambda b: ((b.get("source") or {}).get("type") != "owner", order.get(b.get("area") or "other", 99), b["id"]))
    about = [f"- {b['title']} ({source_note(b, owner_name)}) {ref(b)}" for b in beliefs]
    role = SKILL_ROLE.get(skill)
    lessons = [b for b in await current(db, today=today, kinds=("practice",)) if b["owner"] in (role, None, "system")]
    work = [f"- {b['title']} {ref(b)}" for b in lessons]
    parts = []
    if about:
        parts.append("Om " + owner_name + " (their own words bind; an interpretation is guidance to weigh):\n" + _fit(about, budget * 2 // 3, "find form=belief"))
    if work:
        parts.append("Om arbetet (the office's lessons; one that turns out wrong is replaced, not ignored):\n" + _fit(work, budget // 3, "find form=belief kind=practice"))
    return "\n\n".join(parts)


async def following(db: Database, role: str, now: datetime, tz: str = "Europe/Stockholm", budget: int = BUDGET["following"]) -> str:
    text = await threads.for_review(db, role, now, tz)
    return text if len(text) <= budget else text[:budget].rsplit("\n", 1)[0] + "\n…(the rest: thread <id>, or find)"


async def compose(db: Database, role: str, skill: str, now: datetime, owner_name: str = "ägaren", tz: str = "Europe/Stockholm",
                  count: bool = True) -> dict[str, str]:
    """The three parts for a role's run. `count=False` only looks (for measuring), without counting points as shown."""
    return {
        "known": await known(db, skill, owner_name, tz),
        "following": await following(db, role, now, tz),
        "decide": await docket.render(db, role, now.date(), count=count),
    }


def _fit(lines: list[str], budget: int, fetch: str) -> str:
    out, used = [], 0
    for x in lines:
        if out and used + len(x) > budget:
            return "\n".join(out) + f"\n…({len(lines) - len(out)} till: {fetch})"
        out.append(x)
        used += len(x) + 1
    return "\n".join(out)
