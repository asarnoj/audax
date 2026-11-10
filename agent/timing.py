"""The latest useful moment (instance/granskning/2026-11-10-omgang-14-tid-och-granskning.md, A).

Urgency used to be a level triage set once (low/medium/high) and nothing ever moved it: the Grok meetup, which needed
the organiser's approval and had a waiting list, was planned for the morning of the event, and 25 things went stale on
the shelf without anyone judging them again. Now whoever makes a finding says when the owner must know at the latest,
and why: not the deadline itself but the deadline less what the owner needs to make it. What is urgent follows from that
and from now:
- the desk can't plan past it: a moment after it is moved to it (the last moment before the night, if it falls in it);
- what would hold something past it (the owner's focus, their own count or times) doesn't hold it; the night still does;
- what is saved or planned comes back to the desk once when the moment is a day away;
- what was held until it passed counts as the desk's error ("lät tiden gå"), and against the beliefs it leaned on.
A trigger, not a guarantee (Julius, 9 Nov). The best-before date (`expires_at`) stays: when it is worth nothing at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.schedule import utc_iso

SCHEMA = {
    "latest_useful": {"type": ["string", "null"], "description": "Local 'YYYY-MM-DD HH:MM' (a bare date means by that day): when the owner must "
                      "know at the latest for it to be worth something: not the deadline itself, the deadline less what they need "
                      "to make it (days for an event with approval or a waiting list, an evening for an application). Null when "
                      "nothing runs out (a story, a tip)"},
    "latest_why": {"type": ["string", "null"], "description": "Swedish, one short sentence: why that moment"},
}
REJUDGE = timedelta(days=1)   # what is saved or planned comes back to the desk once when its moment is this close
BEFORE_NIGHT = timedelta(minutes=30)
FALLBACK = timedelta(hours=12)   # without Claude (a quota pause lasts hours), what the owner must know before this goes out   # the last chance before a night is this long before it starts
LET_PASS = ("c.latest_useful IS NOT NULL AND datetime(c.latest_useful) < datetime('now') "
            "AND EXISTS (SELECT 1 FROM decisions d WHERE d.candidate_id = c.id AND d.module LIKE 'dispatcher%') "
            "AND (c.status IN ('shelved', 'expired', 'scheduled') "
            "OR (c.status = 'sent' AND datetime(c.updated_at) > datetime(c.latest_useful)))")


def parse(value, tz: str) -> str | None:
    """A model's 'YYYY-MM-DD HH:MM' (local) as UTC. A bare date means by that day: its start, so the last chance is the
    evening before (last_chance)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("T", " ")
    try:
        d = datetime.fromisoformat(text)
    except ValueError:
        return None
    return utc_iso(d.replace(tzinfo=ZoneInfo(tz)) if d.tzinfo is None else d)


def of(row) -> datetime | None:
    v = row["latest_useful"] if "latest_useful" in row.keys() else None
    return datetime.fromisoformat(v) if v else None


def last_chance(latest: datetime, quiet) -> datetime:
    """The last moment at or before `latest` outside the night: if it falls in the night, the evening before it."""
    if not quiet.contains(latest.time()):
        return latest
    start = latest.replace(hour=quiet.start.hour, minute=quiet.start.minute, second=0, microsecond=0)
    if start > latest:
        start -= timedelta(days=1)
    return start - BEFORE_NIGHT


def show(row, tz: str) -> str:
    """For the desk: the moment, and why."""
    import json

    latest = of(row)
    if latest is None:
        return "-"
    why = json.loads(row["features"] or "{}").get("latest_why")
    from agent.agenda import WEEKDAYS

    d = latest.astimezone(ZoneInfo(tz))
    return f"{WEEKDAYS[d.weekday()]} {d:%d/%m %H:%M}" + (f" ({why})" if why else "")
