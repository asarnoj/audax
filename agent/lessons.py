"""What has been tried before, where it is needed (instance/granskning/2026-11-12-omgang-16-kunskapen.md, "Lärandet").

A lesson is a practice belief (#U, kind practice, agent/beliefs.py): how the office works, with what it rests on. It
comes from a closed matter's point (agent/points.py) or from a role's run, and it can be contradicted and replaced
like any belief.

Writing a lesson down is half of learning; the other half is meeting it when it matters. So when a role creates
something that tries a way of working (an experiment, a rule, a routine, a request, an idea), the code looks for what
is like it and has been tried: closed items of the same form, with what came of them, and the lessons. They come back
with the tool's answer ("a query like this was struck 3 Nov: it brought only full-time jobs"). The role may go ahead;
it does so knowing. Nothing extra is asked of Claude: it is a search.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from agent import trail
from agent.db import Database
from agent.forms import FORMS
from agent.items import Items, ref
from agent.search import search

TRIED = ("experiment", "rule", "routine", "wanted", "aim")   # forms that try a way of working
OUTCOME = ("decision", "outcome", "post_mortem", "reason", "result", "health")
SHOWN = 4


async def tried_before(db: Database, item: dict, role: str) -> str:
    """What is like this new item and has been tried, with what came of it; or ''."""
    if item["form"] not in TRIED:
        return ""
    text = " ".join(str(item.get(k) or "") for k in ("title", "hypothesis", "intervention", "what", "condition", "interpretation"))
    hits = await search(db, text, limit=25, kinds=("item",), exclude_item=item["id"])
    lines, refs = [], []
    items = Items(db)
    for h in hits:
        if h.ref == item["id"] or len(lines) >= SHOWN:
            continue
        other = await items.get(h.ref)
        if other is None:
            continue
        lesson = other["form"] == "belief" and other["kind"] == "practice" and other["status"] == "current"
        ended = other["form"] == item["form"] and other["status"] in FORMS[other["form"]].closed
        if not (lesson or ended):
            continue
        line = f"- {Items.line(other)}"
        if ended:
            what = next((f"{k}: {str(other[k])[:200]}" for k in OUTCOME if other.get(k)), "")
            last = await db.fetchone("SELECT note, created_at FROM item_log WHERE item_id = ? AND status IS NOT NULL ORDER BY id DESC LIMIT 1",
                                     (other["id"],))
            if last and last["note"]:
                what = (what + " · " if what else "") + f"avslutad {last['created_at'][:10]}: {last['note'][:200]}"
            line += f" · {what}" if what else ""
        lines.append(line)
        refs.append(ref(other))
    if not lines:
        return ""
    await trail.emit("tried_before", role, ref=ref(item), shown=refs)
    return ("Tidigare prövat som liknar detta (read before you go on; change or close what you made if it repeats what "
            "failed, and say in a note how it differs):\n" + "\n".join(lines))


async def learn(db: Database, memory: Path | None, role: str, lesson: str, about: list[str], area: str | None = None) -> int:
    """A lesson from a point: a practice belief, linked to what it was learned from."""
    from agent.beliefs import write_practice

    items = Items(db)
    now = datetime.now()
    lid = await items.create("belief", lesson.strip(), role, kind="practice", owner=role,
                             data={"observations": f"{now:%Y-%m-%d}: " + ", ".join(about), **({"subject": area} if area else {})},
                             source={"type": "inferred", "by": role, "date": f"{now:%Y-%m-%d}"}, note="lärdom")
    for r in about:
        if (n := r.lstrip("#")[1:]).isdigit() and await items.get(int(n)):
            await items.link(lid, int(n), "learned_from", role)
    if memory is not None:
        await write_practice(db, memory)
    return lid
