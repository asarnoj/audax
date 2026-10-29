"""What Audax itself takes for granted (instance/granskning/2026-10-29-omgang-7-antaganden.md).

Everything Audax does rests on assumptions about the owner. Those about content (what they want to find) the roles
already hold as beliefs with a source. Those that make up the system itself, how much, how often, what kind, how it
takes initiative, when it stays quiet, used to sit in the settings and the policy as givens, so nobody tested them.
Here they are beliefs like any other: most of them starting assumptions nobody has confirmed (source `assumed`),
which the roles hold against what the owner does and revise, test or ask about.

A few such beliefs are followed by the engine directly (`setting`): the quiet hours. They are changed the way any
belief is, by the owner's word or a role's judgement, and the engine follows the current one.
"""

from __future__ import annotations

import logging
from datetime import datetime, time

from agent.db import Database
from agent.items import Items, ref

log = logging.getLogger(__name__)


def _quiet(value: str):
    from agent.config import QuietHours

    start, end = (time.fromisoformat(x.strip()) for x in value.split("-"))
    return QuietHours(start=start, end=end)


SETTINGS = {"quiet_hours": _quiet}


async def current(db: Database, setting: str) -> dict | None:
    found = await Items(db).find("belief", status="current", where="json_extract(data, '$.setting') = ?", args=(setting,),
                                 order="updated_at DESC", limit=1)
    return found[0] if found else None


async def apply(db: Database, settings) -> None:
    """The engine follows the current belief of each setting."""
    for name, parse in SETTINGS.items():
        b = await current(db, name)
        if b and b.get("value"):
            try:
                setattr(settings, name, parse(b["value"]))
            except (ValueError, TypeError):
                log.warning("belief %s has a %s the engine can't read: %r", ref(b), name, b["value"])


async def ensure(db: Database, settings) -> None:
    """A setting nobody has spoken about yet becomes a starting assumption, so it can be tested and changed."""
    if await current(db, "quiet_hours") is None:
        q = settings.quiet_hours
        await Items(db).create(
            "belief", f"Nattro: inga meddelanden från Audax mellan {q.start:%H:%M} och {q.end:%H:%M}", "system", kind="preference",
            owner="manager", data={"area": "interrupt", "setting": "quiet_hours", "value": f"{q.start:%H:%M}-{q.end:%H:%M}"},
            source={"type": "assumed", "note": "startinställning, inte bekräftad av ägaren"}, note="startantagande")


def watcher(settings):
    """Items watcher: when a belief the engine follows changes, the engine follows the new one."""
    async def watch(db: Database, item: dict) -> None:
        if item["form"] == "belief" and item.get("setting"):
            await apply(db, settings)

    return watch


async def for_review(db: Database, role: str, now: datetime) -> str:
    """For a role's review: the assumptions in its area that nobody has confirmed."""
    mine = [b for b in await Items(db).find("belief", status="current", owner=role, order="updated_at", limit=60)
            if (b.get("source") or {}).get("type") == "assumed"]
    if not mine:
        return ""
    return ("Antaganden ditt arbete vilar på som ingen bekräftat (starting assumptions about the owner, not facts: hold them "
            "against what they do, revise, test or ask when the signals can't tell):\n"
            + "\n".join(f"- {Items.line(b)} · antaget sedan {b['created_at'][:10]}" for b in mine))
