"""What Audax itself takes for granted (instance/granskning/2026-10-29-omgang-7-antaganden.md), and how it is tried
(instance/granskning/2026-10-30-omgang-8-installningar.md).

Everything Audax does rests on assumptions about the owner. Those that make up the experience itself (how much, how
often, together or one by one, how long, when it is quiet) are beliefs like any other, most of them starting
assumptions nobody has confirmed (source `assumed`). The ones that can be counted are typed settings the engine follows
directly: the current belief holds, or a test value while an experiment tries one.

Such an assumption can't be settled by watching: the owner's behaviour answers what Audax already does, so a reserved
setting confirms itself. It is settled by the owner's word, or by trying another value for a while and comparing the
same signals before and during. The code works out where each one stands (untested, being tried, settled), and the
role only chooses how to try it and, when a trial ends, which value holds: no open-ended judgement each night.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from agent.db import Database
from agent.forms import FORMS, SETTINGS as NAMES
from agent.items import Items, ref

log = logging.getLogger(__name__)
UTC = ZoneInfo("UTC")
GAP = timedelta(minutes=2)       # messages this close together are one contact (a long one is sent in parts)
ANSWERED = timedelta(hours=12)   # the owner wrote within this after a contact: it counts as answered
TRIAL_DAYS = (3, 28)


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
    testable: bool                      # may be tried by changing it for a while; if not, only asked about or told


SETTINGS = {s.name: s for s in (
    Setting("quiet_hours", "nattro", _quiet, lambda q: f"{q.start:%H:%M}-{q.end:%H:%M}",
            lambda v: f"Nattro: inga meddelanden från Audax mellan {v.replace('-', ' och ')}", "interrupt", False),
    Setting("daily_contacts", "egna kontakter per dag", _count, str,
            lambda v: f"Högst {v} egna kontakter från Audax om dagen (det som brådskar går ändå fram)", "interrupt", True),
    Setting("bundle_times", "buntar", _times, _hhmm,
            lambda v: f"Fynd som kan vänta går ut samlat kl {v.replace(',', ' och ')}" if v != NONE else "Fynd går ut när de kommer, inte samlade",
            "interrupt", True),
    Setting("message_length", "meddelandelängd", _length, str,
            lambda v: f"Audax egna meddelanden hålls under ungefär {v} tecken", "communication", True),
)}
assert tuple(SETTINGS) == NAMES


async def current(db: Database, setting: str) -> dict | None:
    found = await Items(db).find("belief", status="current", where="json_extract(data, '$.setting') = ?", args=(setting,),
                                 order="updated_at DESC", limit=1)
    return found[0] if found else None


async def trial(db: Database, setting: str | None = None) -> dict | None:
    """The experiment trying a setting now (one at a time), if any."""
    where, args = "json_extract(data, '$.setting') IS NOT NULL", ()
    if setting:
        where, args = "json_extract(data, '$.setting') = ?", (setting,)
    found = await Items(db).find("experiment", status="running", where=where, args=args, order="id", limit=1)
    return found[0] if found else None


def _local_now(settings) -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def _in_window(exp: dict, now: datetime) -> bool:
    return (exp.get("starts") or "") <= f"{now:%Y-%m-%d %H:%M}" and f"{now:%Y-%m-%d}" < (exp.get("review_at") or "")[:10]


async def apply(db: Database, settings, now: datetime | None = None) -> None:
    """The engine follows the current belief of each setting, or the value an experiment is trying while it runs."""
    now = now or _local_now(settings)
    for name, spec in SETTINGS.items():
        b = await current(db, name)
        t = await trial(db, name)
        value = b.get("value") if b else None
        if t and _in_window(t, now):
            value = t.get("test_value")
        if value is None:
            continue
        try:
            setattr(settings, name, spec.parse(value))
        except (ValueError, TypeError):
            log.warning("%s: the engine can't read %s %r", ref(t or b), name, value)


async def ensure(db: Database, settings) -> None:
    """A setting nobody has spoken about yet becomes a starting assumption, so it can be tried and changed."""
    for name, spec in SETTINGS.items():
        if await current(db, name) is None:
            value = spec.show(getattr(settings, name))
            await Items(db).create(
                "belief", spec.title(value), "system", kind="preference", owner="manager",
                data={"area": spec.area, "setting": name, "value": value},
                source={"type": "assumed", "note": "startinställning, inte bekräftad av ägaren"}, note="startantagande")


def watcher(settings):
    """Items watcher: when a setting's belief or a trial changes, the engine follows the new one."""
    async def watch(db: Database, item: dict) -> None:
        if item["form"] in ("belief", "experiment") and item.get("setting"):
            await apply(db, settings)

    return watch


# --- where an assumption stands ----------------------------------------------------------------

def _open(item: dict) -> bool:
    return item["status"] not in FORMS[item["form"]].closed


async def _tried_by(db: Database, belief_id: int) -> list[dict]:
    rows = await db.fetchall("SELECT from_id FROM item_links WHERE to_id = ? AND relation = 'tests' ORDER BY from_id", (belief_id,))
    return [x for x in [await Items(db).get(r["from_id"]) for r in rows] if x]


async def standing(db: Database, belief: dict, today: str) -> tuple[str, str]:
    """('settled' | 'trying' | 'later' | 'untested', in words), worked out from the data, not from any text."""
    if (belief.get("source") or {}).get("type") != "assumed":
        return "settled", "avgjort"
    tried = await _tried_by(db, belief["id"])
    if now_open := [t for t in tried if _open(t)]:
        t = now_open[-1]
        return "trying", f"prövas av {ref(t)}" + (f" till {t['review_at'][:10]}" if t.get("review_at") else "")
    before = "; ".join(f"{ref(t)} {t['status']}" + (f": {t['decision'][:80]}" if t.get("decision") else "")
                       + (f": {t['conclusion'][:80]}" if t.get("conclusion") else "") for t in tried[-3:])
    if belief.get("review_at") and belief["review_at"][:10] > today:
        return "later", f"väntar till {belief['review_at'][:10]}" + (f" · prövat förut: {before}" if before else "")
    return "untested", "oprövat" + (f" · prövat förut: {before}" if before else "")


async def for_review(db: Database, role: str, now: datetime) -> str:
    """For a role's review: where each assumption in its area stands, and the trials that have ended."""
    today = f"{now:%Y-%m-%d}"
    mine = [b for b in await Items(db).find("belief", status="current", owner=role, order="id", limit=80)
            if (b.get("source") or {}).get("type") == "assumed"]
    parts = []
    if mine:
        lines = []
        for b in mine:
            state, words = await standing(db, b, today)
            how = ""
            if b.get("setting"):
                how = f" · inställning {b['setting']}" + ("" if SETTINGS[b["setting"]].testable else ", prövas bara genom att fråga")
            lines.append(f"- {Items.line(b)} · {words}{how}" + (" · VÄLJ HUR" if state == "untested" else ""))
        parts.append(
            "Antaganden ditt arbete vilar på, som ingen bekräftat. They can't be settled by watching: what the owner does "
            "answers what Audax already does. For each marked VÄLJ HUR, choose in `assumptions` how it is tried: test "
            "(a setting: another value for some days; a belief in words: an experiment you create), ask the owner, or "
            "later (a date and why). The owner's own words settle any of them at once.\n" + "\n".join(lines))
    for exp in await Items(db).find("experiment", status="running", owner=role,
                                    where="json_extract(data, '$.setting') IS NOT NULL", order="id"):
        if (exp.get("review_at") or "9999")[:10] <= today:
            parts.append(await _ended(db, exp, now))
    return "\n\n".join(parts)


async def _ended(db: Database, exp: dict, now: datetime) -> str:
    start = datetime.fromisoformat(exp["starts"]).replace(tzinfo=now.tzinfo)
    end = datetime.fromisoformat(exp["review_at"][:10]).replace(tzinfo=now.tzinfo)
    before, during = await measure(db, start - (end - start), start), await measure(db, start, end)
    return (f"Prövningen {ref(exp)} är slut ({exp['title']}; {exp['starts'][:10]}–{exp['review_at'][:10]}), and the setting is "
            "back at its old value. Decide in `settle`: adopt (the tried value holds now), keep (the old one holds, now "
            "tried), or inconclusive. The numbers are one person over a few days: hints, weighed with anything they said.\n"
            + table(before, during))


# --- what the behaviour says, counted -----------------------------------------------------------

def _utc(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _at(s: str) -> datetime:
    return datetime.fromisoformat(s[:19]).replace(tzinfo=UTC)


async def measure(db: Database, start: datetime, end: datetime) -> dict:
    """The same signals for any window, per day where it makes sense: what a trial is judged by."""
    a, b = _utc(start), _utc(end)
    days = max((end - start).total_seconds() / 86400, 1 / 24)
    msgs = await db.fetchall("SELECT direction, origin, created_at, length(text) AS n FROM messages "
                             "WHERE created_at >= ? AND created_at < ? ORDER BY created_at", (a, b))
    contacts, last_own = [], None
    for m in msgs:
        if m["direction"] == "out" and m["origin"] == "proactive":
            if last_own is None or _at(m["created_at"]) - last_own > GAP:
                contacts.append(m)
            last_own = _at(m["created_at"])
    owner = [m for m in msgs if m["direction"] == "in"]
    answered = sum(1 for c in contacts if any(timedelta(0) < _at(o["created_at"]) - _at(c["created_at"]) <= ANSWERED for o in owner))
    started, prev = 0, None
    for m in msgs:
        if m["direction"] == "in" and (prev is None or _at(m["created_at"]) - _at(prev) > timedelta(hours=3)):
            started += 1
        prev = m["created_at"]
    fb = await db.fetchall("SELECT signal, value FROM feedback WHERE created_at >= ? AND created_at < ?", (a, b))
    own_lengths = sorted(m["n"] for m in msgs if m["direction"] == "out" and m["origin"] == "proactive")
    return {
        "dagar": round(days, 1),
        "egna kontakter/dag": round(len(contacts) / days, 1),
        "kontakter som fick svar": f"{answered}/{len(contacts)}",
        "samtal ägaren startade/dag": round(started / days, 1),
        "ägarens meddelanden/dag": round(len(owner) / days, 1),
        "reaktioner": ", ".join(sorted(f["value"] for f in fb if f["signal"] == "reaction")) or "inga",
        "hämtade från det sparade": sum(1 for f in fb if f["signal"] == "shelf_pull"),
        "bad om tyst eller bara det viktiga": sum(1 for f in fb if f["signal"] == "command" and f["value"][:5] in ("quiet", "focus")),
        "egna meddelandens längd (median)": own_lengths[len(own_lengths) // 2] if own_lengths else "-",
    }


def table(before: dict, during: dict) -> str:
    return "\n".join([f"  {'':36} före · under"] + [f"  {k:36} {before[k]} · {during[k]}" for k in before])


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


# --- the role's choices -------------------------------------------------------------------------

ANSWER = {
    "type": "array",
    "description": "How each assumption marked VÄLJ HUR in your review is tried (empty if none).",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "#U…"},
            "plan": {"type": "string", "enum": ["test", "ask", "later"]},
            "value": {"type": ["string", "null"], "description": "test, a setting: the value to try (either direction)"},
            "days": {"type": ["integer", "null"], "description": f"test, a setting: how long, {TRIAL_DAYS[0]}–{TRIAL_DAYS[1]} days"},
            "experiment": {"type": ["string", "null"], "description": "test, a belief in words: the #E you created to try it"},
            "question": {"type": ["string", "null"], "description": "ask: Swedish, what to ask the owner (the Assistant asks in its own words)"},
            "until": {"type": ["string", "null"], "description": "later: YYYY-MM-DD"},
            "why": {"type": "string", "description": "Swedish, one sentence"},
        },
        "required": ["id", "plan", "why"],
    },
}
SETTLE = {
    "type": "array",
    "description": "For each trial of a setting your review says has ended: which value holds now (empty if none).",
    "items": {
        "type": "object",
        "properties": {
            "experiment": {"type": "string", "description": "#E…"},
            "outcome": {"type": "string", "enum": ["adopt", "keep", "inconclusive"]},
            "why": {"type": "string", "description": "Swedish: what in the numbers or their words decided it"},
        },
        "required": ["experiment", "outcome", "why"],
    },
}


def _id(text) -> int | None:
    m = re.search(r"\d+", str(text or ""))
    return int(m.group()) if m else None


async def planned(db: Database, role: str, answer: list | None, settings, now: datetime | None = None) -> list[str]:
    """Carries out the role's choices; returns what happened (and what was refused, and why) for the run's account."""
    now = now or _local_now(settings)
    items, out = Items(db), []
    for p in answer or []:
        b = await items.get(_id(p.get("id")) or 0)
        if b is None or b["form"] != "belief" or b["status"] != "current" or (b.get("source") or {}).get("type") != "assumed":
            out.append(f"{p.get('id')}: inget oprövat antagande, hoppades över")
            continue
        why = (p.get("why") or "").strip()
        if p["plan"] == "test":
            out.append(await _test(db, role, b, p, why, settings, now))
        elif p["plan"] == "ask":
            if not (q := (p.get("question") or "").strip()):
                out.append(f"{ref(b)}: ingen fråga angavs")
                continue
            qid = await items.create("question", q, role, kind="check", owner=role, parent=b.get("parent_id"),
                                     data={"to": "owner", "why": f"prövar antagandet {ref(b)}: {why}", "guess": b["title"],
                                           "settles_it": "ägarens svar"})
            await items.link(qid, b["id"], "tests", role)
            out.append(f"{ref(b)} frågas: #Q{qid}")
        elif p["plan"] == "later":
            try:
                until = date.fromisoformat((p.get("until") or "").strip())
            except ValueError:
                until = None
            if until is None or until <= now.date():
                out.append(f"{ref(b)}: 'later' behöver ett datum framåt")
                continue
            await items.update(b["id"], role, review_at=until.isoformat(), note=f"prövas senare: {why}")
            out.append(f"{ref(b)} prövas tidigast {until}: {why}")
    return out


async def _test(db: Database, role: str, b: dict, p: dict, why: str, settings, now: datetime) -> str:
    items = Items(db)
    if not b.get("setting"):
        e = await items.get(_id(p.get("experiment")) or 0)
        if e is None or e["form"] != "experiment" or not _open(e):
            return f"{ref(b)}: ett antagande i ord prövas med ett experiment du skapat (#E…), inget öppet angavs"
        await items.link(e["id"], b["id"], "tests", role)
        return f"{ref(b)} prövas av {ref(e)}"
    spec = SETTINGS[b["setting"]]
    if not spec.testable:
        return f"{ref(b)}: {spec.label} prövas inte genom att ändra den, fråga i stället"
    try:
        new = spec.show(spec.parse(p.get("value") or ""))
    except (ValueError, TypeError):
        return f"{ref(b)}: kan inte läsa {spec.label} {p.get('value')!r}"
    old = b.get("value") or NONE
    if new == old:
        return f"{ref(b)}: testvärdet är samma som det nuvarande"
    if running := await trial(db):
        return f"{ref(b)}: {ref(running)} prövar redan upplevelsen, en prövning åt gången så resultaten inte blandas ihop"
    days = min(max(int(p.get("days") or 14), TRIAL_DAYS[0]), TRIAL_DAYS[1])
    end = (now + timedelta(days=days)).date()
    eid = await items.create(
        "experiment", f"Prövar {spec.label}: {new} i stället för {old}", role, kind="improve", status="running",
        owner=role, parent=b.get("parent_id"), review_at=end.isoformat(),
        data={"area": spec.name, "hypothesis": why or f"{spec.label} {new} passar bättre än {old}",
              "intervention": f"Motorn använder {spec.label} {new} i stället för {old} till {end}.",
              "measure": "Samma signaler före och under perioden, räknade av motorn; ägarens egna ord väger tyngst.",
              "setting": spec.name, "test_value": new, "starts": f"{now:%Y-%m-%d %H:%M}"})
    await items.link(eid, b["id"], "tests", role)
    await apply(db, settings, now)
    return f"{ref(b)} prövas: {spec.label} {new} till {end} (#E{eid})"


async def settled(db: Database, role: str, answer: list | None, settings, now: datetime | None = None) -> list[str]:
    """Ends the trials the role decided on, and sets which value holds."""
    now = now or _local_now(settings)
    items, out = Items(db), []
    for s in answer or []:
        e = await items.get(_id(s.get("experiment")) or 0)
        if e is None or e["form"] != "experiment" or not e.get("setting") or e["status"] != "running":
            out.append(f"{s.get('experiment')}: ingen pågående prövning av en inställning")
            continue
        spec, why = SETTINGS[e["setting"]], (s.get("why") or "").strip()
        b = await current(db, e["setting"])
        if s["outcome"] == "adopt":
            nid = await items.create("belief", spec.title(e["test_value"]), role, kind="preference", owner=role,
                                     data={"area": spec.area, "setting": spec.name, "value": e["test_value"]},
                                     source={"type": "observed", "note": f"prövat i {ref(e)}: {why}"[:300]})
            if b:
                await items.update(b["id"], role, status="superseded", note=f"ersatt av #U{nid} efter {ref(e)}")
            await items.update(e["id"], role, status="kept", data={"decision": f"antogs: {why}"})
            out.append(f"{ref(e)}: {spec.label} {e['test_value']} gäller nu (#U{nid})")
        elif s["outcome"] == "keep":
            if b:
                await items.update(b["id"], role, source={"type": "observed", "note": f"prövat mot {e['test_value']} i {ref(e)}, behölls: {why}"[:300]},
                                   note=f"prövat i {ref(e)}")
            await items.update(e["id"], role, status="dropped", data={"decision": f"det gamla värdet behölls: {why}"})
            out.append(f"{ref(e)}: {spec.label} {b.get('value') if b else ''} står kvar, nu prövat")
        else:
            await items.update(e["id"], role, status="dropped", data={"decision": f"inget avgjort: {why}"})
            out.append(f"{ref(e)}: inget avgjort, {spec.label} är oprövat igen")
    await apply(db, settings, now)
    return out
