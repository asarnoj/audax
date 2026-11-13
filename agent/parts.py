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

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from agent import docket, threads
from agent.beliefs import HEADINGS, current, source_note
from agent.db import Database
from agent.forms import AREAS
from agent.items import Items, ref

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
# Areas whose lines carry their heading in Det vi vet, because a prompt applies them by that name ("Inte intresserad av").
MARKED = ("not_interested",)
BUDGET = {"known": 6000, "following": 5000, "decide": docket.BUDGET}


async def known(db: Database, skill: str, owner_name: str | None = None, tz: str = "Europe/Stockholm", budget: int = BUDGET["known"]) -> str:
    """Det vi vet: the owner within the skill's areas (their words first), then the role's lessons."""
    from agent.prompts import owner

    owner_name = owner_name or owner()
    today = datetime.now(ZoneInfo(tz)).date()
    areas = SKILL_AREAS.get(skill, None)
    beliefs = [b for b in await current(db, today=today) if b["kind"] != "practice" and b.get("setting") is None]
    if areas is not None:
        beliefs = [b for b in beliefs if (b.get("area") or "other") in areas]
    order = {a: i for i, a in enumerate(AREAS)}
    beliefs.sort(key=lambda b: ((b.get("source") or {}).get("type") != "owner", order.get(b.get("area") or "other", 99), b["id"]))
    about = [f"- {HEADINGS[b['area']] + ': ' if b.get('area') in MARKED else ''}{b['title']} ({source_note(b, owner_name)}) {ref(b)}"
             for b in beliefs]
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


async def compose(db: Database, role: str, skill: str, now: datetime, owner_name: str | None = None, tz: str = "Europe/Stockholm",
                  count: bool = True) -> dict[str, str]:
    """The three parts for a role's run. `count=False` only looks (for measuring), without counting points as shown."""
    return {
        "known": await known(db, skill, owner_name, tz),
        "following": await following(db, role, now, tz),
        "decide": await docket.render(db, role, now.date(), count=count),
    }


# --- the search mission (omgång 16, stage 2) ---------------------------------------------------------

GOAL_KINDS = ("goal", "project")


async def goals_for(db: Database, mission: str, text: str) -> str:
    """The goals a search mission serves, whole (why, what helps and what is noise: what its findings are judged
    against); the rest one line each. A goal serves the mission when it names the mission's id, or the mission names
    it (#S…, a goal or anything under one, which brings its goal along); it is the same words both roles already
    write, so no one keeps a second list. A mission serving none is a point for Researcher (agent/search_points.py)."""
    from agent.pursuits import OPEN

    items = Items(db)
    rows = await items.find("aim", kind=GOAL_KINDS, status=OPEN, order="COALESCE(json_extract(data, '$.deadline'), '9999'), id")
    named = []
    for n in dict.fromkeys(re.findall(r"#S(\d+)", text or "")):
        if (a := await items.get(int(n))) and a["form"] == "aim":
            named.append(a)
    ids = {a["id"] for a in named} | {a["parent_id"] for a in named if a.get("parent_id")}
    served = [r for r in rows if r["id"] in ids or re.search(rf"\b{re.escape(mission)}\b", " ".join(
        str(r.get(k) or "") for k in ("why", "about", "next_step")))]
    served += [a for a in named if a["id"] not in {r["id"] for r in served}]
    out = [f"{ref(r)} {r['title']}" + (f" ({r['status']})" if r["kind"] not in GOAL_KINDS else "") + "".join(
        f"\n  {label}: {r[k]}" for k, label in (("why", "Varför"), ("about", "Om"), ("next_step", "Nästa")) if r.get(k)) for r in served]
    rest = [r for r in rows if r["id"] not in {x["id"] for x in served}]
    if rest:
        out.append(("Other goals:\n" if served else "") + "\n".join(f"- {ref(r)} {r['title']}" for r in rest))
    return "\n".join(out) or "(no open goals)"


async def serves_none(db: Database, mission: str, text: str) -> bool:
    return not (await goals_for(db, mission, text)).startswith("#S")


def _fit(lines: list[str], budget: int, fetch: str) -> str:
    out, used = [], 0
    for x in lines:
        if out and used + len(x) > budget:
            return "\n".join(out) + f"\n…({len(lines) - len(out)} till: {fetch})"
        out.append(x)
        used += len(x) + 1
    return "\n".join(out)
