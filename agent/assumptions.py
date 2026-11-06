"""What Audax takes for granted about the experience (instance/granskning/2026-10-29-omgang-7-antaganden.md), and what
binds (instance/granskning/2026-11-04-omgang-11-kontakten.md).

How much, how often, together or one by one, how long and when it is quiet are beliefs about the owner like any other.
The ones that can be counted are typed (`setting`, `value`), so the engine and the desk can read them.

Only what the owner said binds: a setting whose current belief comes from the owner is a rule the engine follows. The
quiet hours also bind until the owner says otherwise, because a message in the night does harm whatever it is. Every
other value is the office's understanding, guidance for the desk's judgement and not a rule: on 4 Nov a guessed
"three a day" held back the fourth contact of a day however good it was, while the owner fetched about one held-back
thing a day from the shelf. Omgång 8 tried such guesses with trials one at a time; with one person and one to three
messages a day a trial's before-and-during says little and stops everything else, so the evidence is now kept per
contact (agent/ledger.py) and Manager moves its understanding when the evidence points, or asks.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from agent.db import Database
from agent.forms import SETTINGS as NAMES
from agent.items import Items, ref

log = logging.getLogger(__name__)
UTC = ZoneInfo("UTC")
GAP = timedelta(minutes=2)       # messages this close together are one contact (a long one is sent in parts)
ANSWERED = timedelta(hours=12)   # the owner wrote within this after a contact: it counts as answered
PROTECTS = {"quiet_hours"}       # binds until the owner says otherwise, whoever set it


# --- the settings ------------------------------------------------------------------------------

def _quiet(v: str):
    from agent.config import QuietHours

    start, end = (time.fromisoformat(x.strip()) for x in v.split("-"))
    return QuietHours(start=start, end=end)


def _count(v: str) -> int:
    n = int(str(v).strip())
    if not 1 <= n <= 20:
        raise ValueError("1–20")
    return n


NONE = "inga"   # an empty value is dropped when an item is stored, so "no bundles" is a word


def _times(v: str) -> list[time]:
    return sorted({time.fromisoformat(x.strip()) for x in str(v).split(",") if x.strip() and x.strip() != NONE})


def _length(v: str) -> int:
    n = int(str(v).strip())
    if not 150 <= n <= 3000:
        raise ValueError("150–3000")
    return n


def _hhmm(ts: list[time]) -> str:
    return ",".join(f"{t:%H:%M}" for t in ts) or NONE


@dataclass(frozen=True)
class Setting:
    name: str
    label: str                          # Swedish, for the office
    parse: Callable[[str], object]      # the value as text -> what Settings holds (ValueError if it can't be read)
    show: Callable[[object], str]       # what Settings holds -> the value as text
    title: Callable[[str], str]         # the belief's title for a value
    area: str


SETTINGS = {s.name: s for s in (
    Setting("quiet_hours", "nattro", _quiet, lambda q: f"{q.start:%H:%M}-{q.end:%H:%M}",
            lambda v: f"Nattro: inga meddelanden från Audax mellan {v.replace('-', ' och ')}", "interrupt"),
    Setting("daily_contacts", "egna kontakter per dag", _count, str,
            lambda v: f"Ungefär {v} egna kontakter från Audax om dagen brukar passa", "interrupt"),
    Setting("bundle_times", "buntar", _times, _hhmm,
            lambda v: f"Fynd som kan vänta passar bäst samlat kl {v.replace(',', ' och ')}" if v != NONE else "Fynd går ut när de kommer, inte samlade",
            "interrupt"),
    Setting("message_length", "meddelandelängd", _length, str,
            lambda v: f"Audax egna meddelanden landar bäst under ungefär {v} tecken", "communication"),
)}
assert tuple(SETTINGS) == NAMES


async def current(db: Database, setting: str) -> dict | None:
    found = await Items(db).find("belief", status="current", where="json_extract(data, '$.setting') = ?", args=(setting,),
                                 order="updated_at DESC", limit=1)
    return found[0] if found else None


def _from_owner(belief: dict | None) -> bool:
    return bool(belief) and (belief.get("source") or {}).get("type") == "owner"


def binds(settings, name: str) -> bool:
    """Whether the engine follows this setting as a rule now (the owner said it, or it protects)."""
    return name in getattr(settings, "binding", set())


async def apply(db: Database, settings, now: datetime | None = None) -> None:
    """Settings holds each setting's current value, for the engine and the desk to read; which of them bind is kept
    beside them."""
    binding = set(PROTECTS)
    for name, spec in SETTINGS.items():
        b = await current(db, name)
        if b is None or b.get("value") is None:
            continue
        try:
            setattr(settings, name, spec.parse(b["value"]))
        except (ValueError, TypeError):
            log.warning("%s: the engine can't read %s %r", ref(b), name, b["value"])
            continue
        if _from_owner(b):
            binding.add(name)
    settings.binding = binding


async def ensure(db: Database, settings) -> None:
    """A setting nobody has spoken about yet becomes a starting assumption: the office's understanding to begin with."""
    for name, spec in SETTINGS.items():
        if await current(db, name) is None:
            value = spec.show(getattr(settings, name))
            await Items(db).create(
                "belief", spec.title(value), "system", kind="preference", owner="manager",
                data={"area": spec.area, "setting": name, "value": value},
                source={"type": "assumed", "note": "startinställning, inte bekräftad av ägaren"}, note="startantagande")


def watcher(settings):
    """Items watcher: when a setting's belief changes, the engine reads the new one."""
    async def watch(db: Database, item: dict) -> None:
        if item["form"] == "belief" and item.get("setting"):
            await apply(db, settings)

    return watch


async def lines(db: Database, settings) -> str:
    """The settings in words: which bind and which are understanding (for the desk and Manager)."""
    out = []
    for name, spec in SETTINGS.items():
        b = await current(db, name)
        value = b.get("value") if b else spec.show(getattr(settings, name))
        if binds(settings, name):
            why = "the owner's own words" if _from_owner(b) else "protects the owner's night until they say otherwise"
            out.append(f"- {spec.label}: {value} · BINDS ({why})")
        else:
            src = (b.get("source") or {}).get("type") if b else "assumed"
            out.append(f"- {spec.label}: {value} · understanding ({'a guess nobody has confirmed' if src == 'assumed' else src}), "
                       "guidance for judgement, not a rule")
    return "\n".join(out)


# --- what the office takes for granted, for the roles' reviews ----------------------------------

async def for_review(db: Database, role: str, now: datetime, settings=None) -> str:
    """Beliefs in a role's area that nobody has confirmed, so they are held as guesses."""
    mine = [b for b in await Items(db).find("belief", status="current", owner=role, order="id", limit=80)
            if (b.get("source") or {}).get("type") == "assumed"]
    if not mine:
        return ""
    lines_ = [f"- {Items.line(b)} · antaget sedan {b['created_at'][:10]}" for b in mine]
    return ("Antaganden ditt arbete vilar på, som ingen bekräftat. They can't be settled by watching, because what the owner "
            "does answers what Audax already does. Hold them as guesses: vary when there is reason to, look at what has "
            "never been tried, and ask the owner when an answer would tell more. Their own words settle any of them.\n"
            + "\n".join(lines_))


# --- counting --------------------------------------------------------------------------------------

def _utc(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _at(s: str) -> datetime:
    return datetime.fromisoformat(s[:19]).replace(tzinfo=UTC)


async def contacts_today(db: Database, now: datetime) -> int:
    """The office's own contacts so far today (a reminder the owner asked for isn't one)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await db.fetchall(
        "SELECT m.created_at FROM messages m WHERE m.direction = 'out' AND m.origin = 'proactive' AND m.created_at >= ? "
        "AND NOT EXISTS (SELECT 1 FROM message_refs r JOIN bookings k ON r.ref = '#B' || k.id "
        "WHERE r.message_id = m.id AND k.kind = 'reminder') ORDER BY m.created_at", (_utc(midnight),))
    n, last = 0, None
    for r in rows:
        if last is None or _at(r["created_at"]) - last > GAP:
            n += 1
        last = _at(r["created_at"])
    return n


def next_bundle(times: list[time], now: datetime, grace: timedelta = timedelta(minutes=15)) -> datetime | None:
    """None if now is a bundle moment (or there are no bundles); else the next one."""
    if not times:
        return None
    for day in (0, 1):
        for t in times:
            at = (now + timedelta(days=day)).replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if at <= now < at + grace:
                return None
            if at > now:
                return at
    return None


# --- Manager's understanding ---------------------------------------------------------------------

UNDERSTANDING = {
    "type": "array",
    "description": "When the evidence (or something worth learning) moves your understanding of a setting: the new value "
                   "and why. Not for one the owner set: ask them instead. Usually empty.",
    "items": {
        "type": "object",
        "properties": {
            "setting": {"type": "string", "enum": [n for n in NAMES if n not in PROTECTS]},
            "value": {"type": "string", "description": "daily_contacts: a number; bundle_times: HH:MM,HH:MM or inga; message_length: characters"},
            "why": {"type": "string", "description": "Swedish: what in the ledger or their words points there, or what you want to learn"},
        },
        "required": ["setting", "value", "why"],
    },
}


def _id(text) -> int | None:
    m = re.search(r"\d+", str(text or ""))
    return int(m.group()) if m else None


async def understood(db: Database, role: str, answer: list | None, settings) -> list[str]:
    """Moves the understanding of a setting: a new belief from the evidence, the old one superseded."""
    items, out = Items(db), []
    for u in answer or []:
        spec = SETTINGS.get(u.get("setting") or "")
        if spec is None or spec.name in PROTECTS:
            continue
        old = await current(db, spec.name)
        if _from_owner(old):
            out.append(f"{spec.label}: ägaren har själv sagt {old.get('value')}; fråga hellre än att ändra")
            continue
        try:
            value = spec.show(spec.parse(u.get("value") or ""))
        except (ValueError, TypeError):
            out.append(f"{spec.label}: kan inte läsa {u.get('value')!r}")
            continue
        if old and old.get("value") == value:
            continue
        why = (u.get("why") or "").strip()
        nid = await items.create("belief", spec.title(value), role, kind="preference", owner=role,
                                 data={"area": spec.area, "setting": spec.name, "value": value},
                                 source={"type": "inferred", "note": why[:300] or "ur kontaktliggaren"})
        if old:
            await items.update(old["id"], role, status="superseded", note=f"ersatt av #U{nid}: {why}"[:300])
        out.append(f"{spec.label}: {value} (#U{nid}): {why}")
    await apply(db, settings)
    return out


async def end_trials(db: Database) -> list[str]:
    """Once: the trials of omgång 8 end; the settings are understanding now, and the ledger keeps the evidence."""
    if await db.kv_get("assumptions.trials_ended"):
        return []
    done = []
    items = Items(db)
    for e in await items.find("experiment", status="running", where="json_extract(data, '$.setting') IS NOT NULL"):
        await items.update(e["id"], "system", status="dropped",
                           data={"decision": "avslutad 4 nov: prövningarna ersattes av kontaktliggaren och bedömning (omgång 11)"})
        done.append(ref(e))
    await db.kv_set("assumptions.trials_ended", True)
    return done


async def tell_trials_ended(db: Database) -> int | None:
    """Once: Manager hears that its trials ended. They were dropped by the code, so its working memory went on saying a
    trial was running and deferring to it (#E201, 5 and 6 Nov); a change made under a role is news for that role."""
    if await db.kv_get("assumptions.trials_told"):
        return None
    await db.kv_set("assumptions.trials_told", True)
    ended = await db.fetchall(
        "SELECT id FROM items WHERE form = 'experiment' AND status = 'dropped' "
        "AND json_extract(data, '$.decision') LIKE 'avslutad 4 nov: prövningarna ersattes%'")
    if not ended:
        return None
    from agent.posts import Posts

    refs = ", ".join(f"#E{r['id']}" for r in ended)
    return await Posts(db).send(
        "system", "manager", "note",
        f"Prövningarna av inställningarna är avslutade sedan 4 nov ({refs}, status dropped), och protokollet med en prövning "
        "i taget, fasta perioder och avgörande finns inte längre. Inget väntar på att de ska avgöras. Inställningarna är din "
        "förståelse (understanding) och vägleder desken utan att binda; kontaktliggaren under Measures håller underlaget per kontakt.")
