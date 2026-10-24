"""The calendar and the review (instance/granskning/2026-10-20-omgang-1-grunden.md, "Motorerna"): what is due, who
should be woken for it, and what each role should tidy.

Nothing runs forever: every step, experiment and question has a date it is looked at again (agent/forms.py). When
that date comes, the calendar wakes the role that owns it, or, for a step the Assistant owns that tells or asks the
owner something, has the Assistant do it at once. A question the owner hasn't answered in a week is closed as
unanswered, which is something to understand rather than to ask again. The review lays out, for a role's run, what
is due, what has stalled, what was never judged, and the balance between improving and exploring.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.db import Database
from agent.forms import FORMS
from agent.items import Items, parse_ref, ref

log = logging.getLogger(__name__)

UNANSWERED_AFTER = timedelta(days=7)
STALLED_AFTER = timedelta(days=7)
OWNER_FACING = ("tell_owner", "ask_owner")


def stamp(now: datetime) -> str:
    """How review_at is compared: dates ('2026-10-21') and times ('2026-10-21 12:00') in local time sort together."""
    return f"{now:%Y-%m-%d %H:%M}"


async def due_items(db: Database, now: datetime, role: str | None = None) -> list[dict]:
    """Open items whose date has come."""
    return await Items(db).find(open_only=True, owner=role, where="review_at IS NOT NULL AND review_at <= ?", args=(stamp(now),),
                                order="review_at, id")


def describe(item: dict) -> str:
    extra = []
    for k in ("action", "done_when", "after", "hypothesis", "measure", "to", "why"):
        if item.get(k):
            extra.append(f"{k}: {item[k]}")
    return Items.line(item) + f" · dags {item['review_at']}" + (" · " + " · ".join(extra) if extra else "")


async def answered_since(db: Database, role: str, since: str) -> list[dict]:
    """Questions a role asked that got an answer since its last run: the answer goes back to who asked."""
    return await Items(db).find("question", status=("answered", "unanswered"), owner=role, where="updated_at >= ?", args=(since,))


async def review(db: Database, role: str, now: datetime, memory=None) -> str:
    """What a role should look at in its run and tidy in its cleaning pass. Swedish headings, English notes."""
    items = Items(db)
    parts = []
    due = await due_items(db, now, role)
    if due:
        parts.append("Dags nu (due; act on each, or move its date with a reason):\n" + "\n".join(f"- {describe(i)}" for i in due))
    stalled = await items.find("step", status=("doing", "waiting"), owner=role, where="updated_at < datetime('now', ?)",
                               args=(f"-{STALLED_AFTER.days} days",))
    stalled = [s for s in stalled if s not in due]
    if stalled:
        parts.append("Steg som stannat (stalled a week or more):\n" + "\n".join(f"- {describe(i)}" for i in stalled))
    running = await items.find("experiment", status=("planned", "running", "evaluating"), owner=role)
    if running:
        parts.append("Experiment som pågår:\n" + "\n".join(f"- {describe(i)}" for i in running))
    recent = await items.find("experiment", owner=role, where="created_at >= datetime('now', '-21 days')")
    if role in ("director", "manager", "researcher"):
        n_explore = sum(1 for e in recent if e["kind"] == "explore")
        parts.append(f"Balans senaste tre veckorna: {len(recent) - n_explore} förbättra, {n_explore} utforska.")
    waiting = await items.find("question", status=("open", "asked"), owner=role)
    if waiting:
        parts.append("Frågor som väntar på svar:\n" + "\n".join(f"- {describe(i) if i['review_at'] else Items.line(i)}" for i in waiting))
    expired = await items.find("belief", status="current", owner=role, where="valid_until IS NOT NULL AND valid_until < ?",
                               args=(f"{now:%Y-%m-%d}",))
    if expired:
        parts.append("Uppfattningar som passerat sitt datum (retract or renew):\n" + "\n".join(f"- {Items.line(i)}" for i in expired))
    if memory is not None:
        from agent.texts import oversized

        if big := oversized(memory, role):
            parts.append("Texter som vuxit förbi sin storlek (tighten: refer by id instead of copying):\n" + "\n".join(f"- {b}" for b in big))
    if role == "researcher":
        parts.append(await source_yield(db))
    rules = await items.find("rule", status="active", owner=role)
    if rules:
        parts.append("Regler (hits = how often each threw something away; one that never hits, or stops a lot, is worth a look):\n"
                     + "\n".join(f"- {Items.line(r)} · hits {r.get('hits') or 0}" for r in rules))
    return "\n\n".join(parts) or "Inget att städa just nu."


LOW_YIELD_AFTER = 20   # findings read before a source that has kept nothing is worth questioning


async def source_yield(db: Database, days: int = 30) -> str:
    """Each watched source's last month: what it brought, what was kept, what reached the owner; the weak and the
    paused ones flagged, so Researcher can tune, pause or replace them (the LessWrong lesson, 20 Oct)."""
    from agent import routines

    rows = {r["source"]: r for r in await db.fetchall(
        "SELECT e.source, count(DISTINCT e.id) AS n, count(c.id) AS judged, "
        "sum(c.status NOT IN ('dropped', 'expired')) AS kept, sum(c.status = 'sent') AS sent "
        "FROM events e LEFT JOIN candidates c ON c.event_id = e.id "
        "WHERE e.source LIKE 'stream:%' AND e.created_at >= datetime('now', ?) GROUP BY e.source", (f"-{days} days",))}
    lines = []
    for r in await routines.all_routines(db, "stream"):
        sid = r["_config"]["stream"]["id"]
        y = rows.get(f"stream:{sid}")
        n, kept, sent = (y["n"], y["kept"] or 0, y["sent"] or 0) if y else (0, 0, 0)
        flag = ""
        if r["status"] != "active":
            flag = f" · {r['status']}" + (f" ({r['health']})" if r.get("health") else "")
        elif n >= LOW_YIELD_AFTER and kept == 0:
            flag = " · low yield"
        elif n == 0:
            flag = " · nothing in a month"
        lines.append(f"- {ref(r)} {sid}: {n} in, {kept} kept, {sent} sent{flag}")
    if not lines:
        return "Källor: inga bevakningar ännu."
    return f"Källor senaste {days} dagarna (in · kept · sent):\n" + "\n".join(lines)


class Calendar:
    """Ticks every few minutes: wakes the owners of what is due, once per date."""

    def __init__(self, db: Database, tz: str, wake: Callable[[str, str], None], speak=None,
                 quiet: Callable[[], Awaitable[bool]] | None = None):
        self.db = db
        self.tz = ZoneInfo(tz)
        self.wake = wake            # (role, reason) -> schedules that role's run
        self.speak = speak          # the chat's speak: for steps that tell or ask the owner
        self.quiet = quiet          # async () -> True during quiet hours or when the owner asked for quiet

    async def tick(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(self.tz)
        done: list[str] = []
        items = Items(self.db)
        for q in await items.find("question", status="asked", where="json_extract(data, '$.asked_at') < ?",
                                  args=(f"{now - UNANSWERED_AFTER:%Y-%m-%dT%H:%M}",)):
            await items.update(q["id"], "system", status="unanswered", note="inget svar på en vecka")
            done.append(f"{ref(q)} obesvarad")
        roles: dict[str, list[str]] = {}
        for item in await due_items(self.db, now):
            key = f"due.woken.{item['id']}"
            if await self.db.kv_get(key) == item["review_at"]:
                continue   # already woken for this date
            if item["form"] == "step" and item["owner"] == "assistant" and item.get("action") in OWNER_FACING:
                if self.speak is None or (self.quiet and await self.quiet()):
                    continue   # later, when the owner can be reached
                if not await self._tell(item):
                    continue
            elif item["owner"]:
                roles.setdefault(item["owner"], []).append(ref(item))
            await self.db.kv_set(key, item["review_at"])
            done.append(f"{ref(item)} → {item['owner']}")
        for role, refs in roles.items():
            self.wake(role, "dags: " + ", ".join(refs))
        if done:
            log.info("calendar: %s", "; ".join(done))
        return done

    async def _tell(self, item: dict) -> bool:
        from agent.pipeline.outbox import AlreadySaid

        items = Items(self.db)
        parent = await items.get(item["parent_id"]) if item["parent_id"] else None
        what = "ask the owner" if item["action"] == "ask_owner" else "tell the owner"
        waited = await items.get(parse_ref(item["after"])) if item.get("after") and parse_ref(item["after"]) else None
        if waited is not None:   # a promise that waited for something: say what came of it, or where it stands
            over = waited["status"] in FORMS[waited["form"]].closed
            instruction = (f"You promised the owner to come back about this: {item['title']}. It waited for {ref(waited)} "
                           f"({waited['title']}), which is " + ("now done" if over else f"not done yet ({waited['status']})") + ". "
                           f"Read {ref(waited)} for what came of it" + ("" if over else " and where it stands, and say so honestly")
                           + ", and tell them briefly, in your own words.")
        else:
            instruction = (f"It is time for a step of a plan: {what} now. Step: {item['title']}. Done when: {item.get('done_when', '')}."
                           + (f" It serves: {parent['title']}." if parent else "")
                           + " Look it up (read the step and what it serves) if you need the details; write it in your own words, and "
                           "say briefly why it matters to them now.")
        try:
            ids = await self.speak(instruction, about={ref(item)} | ({ref(waited)} if waited else set()))
        except AlreadySaid:
            ids = [0]
        except Exception as e:
            log.warning("couldn't do step %s: %s", ref(item), e)
            return False
        if not ids:
            return False
        await Items(self.db).update(item["id"], "assistant", status="done", data={"result": "Sagt till ägaren."}, note="levererat")
        return True



NOW_DATES = "## Datum (skrivs av koden)"


async def dates_text(db: Database, now: datetime, days: int = 60) -> str:
    """Deadlines, steps with dates and bookings ahead, from layer 2: added to now.md by the code, so a rewrite of the
    file can't lose them (the Google section vanished in a test on 19 Oct)."""
    until = f"{now + timedelta(days=days):%Y-%m-%d}"
    rows: list[tuple[str, str]] = []
    for a in await Items(db).find("aim", open_only=True, where="json_extract(data, '$.deadline') BETWEEN ? AND ?",
                                  args=(f"{now:%Y-%m-%d}", until)):
        rows.append((a["deadline"], f"sista dag för {ref(a)} {a['title']}"))
    for s in await Items(db).find("step", open_only=True, where="review_at IS NOT NULL AND review_at <= ?", args=(until,)):
        rows.append((s["review_at"], f"steg {ref(s)} ({s['owner']}): {s['title']}"))
    utc_until = (now + timedelta(days=days)).astimezone(ZoneInfo("UTC")).isoformat()
    for b in await db.fetchall("SELECT id, due_at, title FROM bookings WHERE status = 'planned' AND due_at <= ? ORDER BY due_at", (utc_until,)):
        local = datetime.fromisoformat(b["due_at"]).astimezone(now.tzinfo)
        rows.append((f"{local:%Y-%m-%d %H:%M}", f"bokning #B{b['id']}: {b['title']}"))
    rows.sort()
    return NOW_DATES + "\n" + ("\n".join(f"- {when}: {what}" for when, what in rows) or "- Inget med datum de närmaste två månaderna.")


async def write_now(db: Database, path, now: datetime, text: str | None = None) -> None:
    """now.md: Director's own words (text, or what is there) and then the dates, always fresh."""
    own = text if text is not None else (path.read_text() if path.exists() else "# Nu\n")
    own = own.split(NOW_DATES)[0].rstrip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(own + "\n\n" + await dates_text(db, now) + "\n")



def starter(wake: Callable[[str, str], None]):
    """An Items watcher: a research step another role (or the code) gives Researcher wakes it at once. The step's date
    is when the answer is needed at the latest, not when the work starts (22 Oct: an improv question asked on 16 Oct
    had a step dated 4 Nov, and nothing happened until then). Spaced by the waker, so it can't loop."""

    async def start(db: Database, item: dict) -> None:
        if (item["form"] == "step" and item["owner"] == "researcher" and item["status"] == "planned"
                and item["by"] != "researcher" and item["created_at"] == item["updated_at"]):
            wake("researcher", f"nytt steg {ref(item)}: {item['title']}")

    return start


def follower(tz: str):
    """An Items watcher: when something is over, the open steps that waited for it are due now, so a promise like
    "jag hör av mig när planen är klar" is kept when it happens rather than at a guessed time."""
    zone = ZoneInfo(tz)

    async def follow(db: Database, item: dict) -> None:
        if item["status"] not in FORMS[item["form"]].closed:
            return
        items = Items(db)
        now = stamp(datetime.now(zone))
        for step in await items.find("step", open_only=True, where="json_extract(data, '$.after') = ?", args=(ref(item),)):
            if not step["review_at"] or step["review_at"] > now:
                await items.update(step["id"], "system", review_at=now, note=f"{ref(item)} är klart")

    return follow


async def earlier_runs(db: Database, role: str, tz: str, n: int = 3) -> str:
    """What the role concluded in its last slow runs, so a run takes up the thread and thinks on from it instead of
    starting over (24 Oct: "det är ett pågående arbete")."""
    from agent.situation import FAST
    from agent.vocab import kinds_of, task_name

    kinds = [k for k in kinds_of(role) if k not in FAST and k != "chat"]
    if not kinds:
        return ""
    rows = await db.fetchall(
        f"SELECT kind, finished_at, summary FROM claude_runs WHERE kind IN ({','.join('?' * len(kinds))}) AND summary IS NOT NULL "
        "AND is_error = 0 ORDER BY id DESC LIMIT ?", (*kinds, n))
    zone = ZoneInfo(tz)
    lines = []
    for r in rows:
        when = datetime.fromisoformat(r["finished_at"][:19] + "+00:00").astimezone(zone)
        lines.append(f"- {when:%d/%m} {task_name(r['kind']).lower()}: {' '.join(r['summary'].split())[:900]}")
    return ("\n\n### Dina senaste körningar (vad du kom fram till: ta upp tråden och tänk vidare)\n" + "\n".join(lines)) if lines else ""
