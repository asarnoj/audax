"""Threads (instance/granskning/2026-10-26-omgang-6-kontakt.md): everything the office knows about one thing, so a role
can judge for itself when and what to ask, instead of a schedule or a threshold deciding for it.

Any item a role judges important for its work can be followed: a goal, a belief about the owner's character, a
hypothesis, an experiment. Following it means the role has said when it means to look at it again, and why. The
thread is the view the code builds around the item: what it is, its timeline (what changed, what the owner said
about it, what was sent about it), every time the office brought it up and how that landed, what hangs together with
it, and what is coming in the world (a deadline, an opening).

A thread comes up on the date its follower set, or earlier when something new happens in it: the owner mentions it,
another role changes it or adds to it, a deadline comes close. The follower reads the whole thread and decides: ask
(which angle, which detail, which suggestion), wait (a new date and why), or do something else.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.db import Database
from agent.forms import FORMS, LETTERS
from agent.items import Items, ref

log = logging.getLogger(__name__)
UTC = ZoneInfo("UTC")
SOON = timedelta(days=3)   # a deadline this close is news in a thread


def _stamp(now: datetime) -> str:
    return f"{now:%Y-%m-%d %H:%M}"


def _local(sqlite_utc: str | None, tz: ZoneInfo) -> str:
    if not sqlite_utc:
        return "?"
    return f"{datetime.fromisoformat(sqlite_utc[:19] + '+00:00').astimezone(tz):%d/%m}"


# --- following -----------------------------------------------------------------------------

async def follow(db: Database, role: str, item_id: int, look_at: str, why: str) -> None:
    """Starts or renews following an item: the news so far is read, so it is cleared."""
    await db.execute(
        "INSERT INTO follows (role, item_id, why, look_at) VALUES (?, ?, ?, ?) ON CONFLICT(role, item_id) DO UPDATE SET "
        "why = excluded.why, look_at = excluded.look_at, news = NULL, status = 'active', updated_at = datetime('now')",
        (role, item_id, why, look_at))


async def unfollow(db: Database, role: str, item_id: int, why: str) -> bool:
    return bool(await db.execute_count(
        "UPDATE follows SET status = 'ended', why = why || ' · slutade följa: ' || ?, updated_at = datetime('now') "
        "WHERE role = ? AND item_id = ? AND status = 'active'", (why, role, item_id)))


async def followed(db: Database, role: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM follows WHERE status = 'active'", []
    if role:
        sql += " AND role = ?"; args.append(role)
    return [dict(r) for r in await db.fetchall(sql + " ORDER BY look_at", tuple(args))]


async def news(db: Database, item_id: int, what: str, by: str | None, now: datetime) -> None:
    """Something new in a thread: its followers (other than whoever made the change) look at it now."""
    for f in await db.fetchall("SELECT * FROM follows WHERE item_id = ? AND status = 'active'", (item_id,)):
        if by and f["role"] == by:
            continue
        lines = [x for x in (f["news"] or "").splitlines() if x][-4:] + [f"{now:%d/%m} {what}"]
        await db.execute("UPDATE follows SET news = ?, look_at = min(look_at, ?), updated_at = datetime('now') WHERE id = ?",
                         ("\n".join(lines), _stamp(now), f["id"]))


async def due(db: Database, now: datetime) -> list[dict]:
    return [dict(r) for r in await db.fetchall(
        "SELECT * FROM follows WHERE status = 'active' AND look_at <= ? ORDER BY look_at", (_stamp(now),))]


def watcher(tz: str):
    """Items watcher: a change to a followed item, or to something under it, is news for its followers."""
    zone = ZoneInfo(tz)

    async def watch(db: Database, item: dict) -> None:
        last = await db.fetchone("SELECT by, note, status FROM item_log WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item["id"],))
        by = last["by"] if last else item.get("by")
        what = f"{ref(item)} {item['title'][:60]}: {(last['note'] if last and last['note'] else item['status'])}"[:160]
        now = datetime.now(zone)
        await news(db, item["id"], what, by, now)
        if item.get("parent_id"):
            await news(db, item["parent_id"], what, by, now)

    return watch


async def mentioned(db: Database, message_ids: list[int], refs: set[str], now: datetime) -> None:
    """The owner's own words linked to a followed item are news in its thread (agent/refs.py)."""
    rows = await db.fetchall(f"SELECT id, text FROM messages WHERE direction = 'in' AND id IN ({','.join('?' * len(message_ids))})",
                             tuple(message_ids)) if message_ids else []
    if not rows:
        return
    for r in refs:
        letter, num = r.lstrip("#")[:1], r.lstrip("#")[1:]
        if letter in LETTERS and num.isdigit():
            await news(db, int(num), f"ägaren: \"{rows[-1]['text'][:100]}\"", "owner", now)


async def deadlines(db: Database, now: datetime) -> None:
    """A followed item whose deadline (or its opportunity's) comes close is news, once."""
    until = f"{now + SOON:%Y-%m-%d}"
    for f in await followed(db):
        item = await Items(db).get(f["item_id"])
        if item is None:
            continue
        day = item.get("deadline") or item.get("date")
        if day and f"{now:%Y-%m-%d}" <= day <= until and f"sista dag {day}" not in (f["news"] or ""):
            await news(db, item["id"], f"sista dag {day} närmar sig", None, now)


# --- what a contact came to ---------------------------------------------------------------

async def outcome(db: Database, message_id: int) -> str:
    """How a message from the office landed: a reaction, a reply, likely an answer (the owner wrote within half a
    day), or nothing."""
    fb = await db.fetchall("SELECT signal, value FROM feedback WHERE message_id = ?", (message_id,))
    reactions = [f["value"] for f in fb if f["signal"] == "reaction"]
    if any(f["signal"] == "reply" for f in fb):
        return "svarade på det" + (f" · reaktion {' '.join(reactions)}" if reactions else "")
    if reactions:
        return f"reaktion {' '.join(reactions)}"
    m = await db.fetchone("SELECT created_at FROM messages WHERE id = ?", (message_id,))
    if m and await db.fetchone("SELECT 1 FROM messages WHERE direction = 'in' AND created_at > ? AND created_at <= datetime(?, '+12 hours')",
                               (m["created_at"], m["created_at"])):
        return "skrev efteråt (troligen svar)"
    if m and datetime.fromisoformat(m["created_at"][:19] + "+00:00") > datetime.now(UTC) - timedelta(hours=12):
        return "för tidigt att säga"
    return "inget svar"


async def contacts(db: Database, refs: set[str], tz: ZoneInfo, limit: int = 6) -> list[str]:
    """Every time the office brought these things up with the owner, and how it landed."""
    if not refs:
        return []
    rows = await db.fetchall(
        f"SELECT DISTINCT m.id, m.created_at, m.text FROM messages m JOIN message_refs r ON r.message_id = m.id "
        f"WHERE r.ref IN ({','.join('?' * len(refs))}) AND m.direction = 'out' ORDER BY m.id DESC LIMIT ?", (*refs, limit))
    return [f"- {_local(r['created_at'], tz)}: \"{' '.join(r['text'].split())[:140]}\" → {await outcome(db, r['id'])}" for r in reversed(rows)]


# --- the thread ----------------------------------------------------------------------------

async def thread(db: Database, item_id: int, tz: str = "Europe/Stockholm") -> str | None:
    """Everything about one thing, for the role that follows it."""
    zone = ZoneInfo(tz)
    items = Items(db)
    item = await items.get(item_id)
    if item is None:
        return None
    parts = [await items.describe(item_id) or Items.line(item)]
    follows = await db.fetchall("SELECT role, why, look_at, news FROM follows WHERE item_id = ? AND status = 'active'", (item_id,))
    for f in follows:
        parts.append(f"Följs av {f['role']}: {f['why']} · ses över {f['look_at']}" + (f"\nNytt sedan dess:\n{f['news']}" if f["news"] else ""))
    children = await items.find(parent=item_id, order="id", limit=30)
    links = await db.fetchall("SELECT from_id, to_id, relation FROM item_links WHERE from_id = ? OR to_id = ?", (item_id, item_id))
    related = children + [x for x in [await items.get(l["to_id"] if l["from_id"] == item_id else l["from_id"]) for l in links] if x]
    if related:
        parts.append("Hör ihop med:\n" + "\n".join(
            f"- {Items.line(r)}" + (f" · svar: {r['answer'][:120]}" if r.get("answer") else "")
            + (f" · resultat: {r['result'][:120]}" if r.get("result") else "")
            + (f" · {r['deadline']}" if r.get("deadline") else "") for r in related[:15]))
    refs = {ref(item)} | {ref(r) for r in related}
    said = await db.fetchall(
        f"SELECT DISTINCT m.created_at, m.text FROM messages m JOIN message_refs r ON r.message_id = m.id "
        f"WHERE r.ref IN ({','.join('?' * len(refs))}) AND m.direction = 'in' ORDER BY m.id DESC LIMIT 5", tuple(refs))
    if said:
        parts.append("Vad ägaren sagt om det:\n" + "\n".join(f"- {_local(s['created_at'], zone)}: \"{' '.join(s['text'].split())[:200]}\"" for s in reversed(said)))
    if c := await contacts(db, refs, zone):
        parts.append("När kontoret tagit upp det, och hur det landade (don't ask the same again; a question that got no answer says something too):\n" + "\n".join(c))
    world = [f"- {ref(r)} {r['title'][:70]}: sista dag {r['deadline']}" for r in [item, *related] if r.get("deadline")]
    world += [f"- {ref(r)} {r['title'][:70]}: datum {r['date']}" for r in [item, *related] if r.get("date")]
    if world:
        parts.append("Det som väntar i världen:\n" + "\n".join(dict.fromkeys(world)))
    return "\n\n".join(parts)


async def for_review(db: Database, role: str, now: datetime, tz: str = "Europe/Stockholm") -> str:
    """For a role's review: the threads that have come up (whole), and the rest it follows (one line each)."""
    mine = await followed(db, role)
    if not mine:
        return ""
    stamp = _stamp(now)
    up = [f for f in mine if f["look_at"] <= stamp]
    rest = [f for f in mine if f["look_at"] > stamp]
    parts = []
    for f in up[:4]:
        if text := await thread(db, f["item_id"], tz):
            parts.append(f"Tråd att se över nu ({'något nytt har hänt' if f['news'] else 'ditt datum'}):\n{text}")
    if up:
        parts[0:0] = ["Trådar som kommit upp: read each, then decide. Ask (find_out, ask owner: which angle, detail or suggestion "
                      "is worth it now), act, or wait. Either way, `follow` again with the date you'll look next and why, or stop following."]
    if rest:
        parts.append("Du följer också:\n" + "\n".join(f"- #{FORMS[(await Items(db).get(f['item_id']))['form']].letter}{f['item_id']}: {f['why'][:120]} · ses över {f['look_at']}"
                                                         for f in rest[:20]))
    return "\n\n".join(parts)
