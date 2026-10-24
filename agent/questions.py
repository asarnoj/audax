"""What the office wants to know (questions, #Q, agent/forms.py), and the paths an answer comes back by.

A role that notices something unclear that matters registers it with its best guess and what would settle it, and
chooses where the answer comes from (24 Oct: rather than forcing questions, give the roles a construct that makes
finding out natural):
- owner: the Assistant asks it in its own words when it fits (agent/chat.py), and records the answer;
- another role: it sees the question in its review and answers it;
- world: the code makes a research step for Researcher at once, and the step's result is the answer;
- self: an experiment or a signal to wait for, with the question as its parent; the experiment's outcome is the answer.
The answer comes back to the one who asked, next to its guess, and it writes what it meant (the conclusion).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from agent.db import Database
from agent.forms import FORMS
from agent.items import Items, ref

ANSWERED_BY = {"step": ("result",), "experiment": ("outcome", "decision")}


async def route(db: Database, item: dict) -> None:
    """Items watcher: a new question to the world becomes a research step; a closed step or experiment that served a
    question answers it."""
    items = Items(db)
    if item["form"] == "question" and item.get("to") == "world" and item["status"] == "open" and item["created_at"] == item["updated_at"]:
        when = item["review_at"] or f"{datetime.now() + timedelta(days=3):%Y-%m-%d}"
        step = await items.create("step", f"Ta reda på: {item['title']}"[:200], "system", owner="researcher", parent=item["id"],
                                  review_at=when, data={"action": "research", "done_when": item.get("settles_it") or "Ett svar med källa"},
                                  note=f"{item['owner']} vill veta det: {item.get('why') or ''}"[:300])
        await items.update(item["id"], "system", status="asked", note=f"Researcher tar reda på det (#T{step})")
        return
    if item["form"] in ANSWERED_BY and item["status"] in FORMS[item["form"]].closed and item["parent_id"]:
        q = await items.get(item["parent_id"])
        if q is None or q["form"] != "question" or q["status"] in FORMS["question"].closed:
            return
        answer = " · ".join(str(item[k]) for k in ANSWERED_BY[item["form"]] if item.get(k)) or f"{ref(item)} blev {item['status']}"
        await items.update(q["id"], "system", status="answered", data={"answer": answer[:2000]}, note=f"svar från {ref(item)}")


async def wondering(db: Database, role: str) -> str:
    """For a role's review: what it wants to know, with its guesses, and what other roles have asked it."""
    items = Items(db)
    parts = []
    mine = await items.find("question", status=("open", "asked"), owner=role, order="id")
    if mine:
        parts.append("Det du vill veta (your open questions, with your guess; when the answer comes you say what it meant):\n" + "\n".join(
            f"- {ref(q)} till {q.get('to')}: {q['title']} · {q['status']}" + (f" · gissning: {q['guess']}" if q.get("guess") else "")
            + (f" · avgörs av: {q['settles_it']}" if q.get("settles_it") else "") for q in mine))
    to_me = await items.find("question", status=("open", "asked"), where="json_extract(data, '$.to') = ?", args=(role,), order="id")
    if to_me:
        parts.append("Frågor till dig från andra roller (answer with update: fields.answer, status answered):\n" + "\n".join(
            f"- {ref(q)} från {q['owner']}: {q['title']} · varför: {q.get('why') or '-'}" for q in to_me))
    return "\n\n".join(parts)
