"""The contact ledger (instance/granskning/2026-11-04-omgang-11-kontakten.md): every contact of the office's own, with
what it was and how it landed, so the desk and Manager judge from evidence instead of following guessed numbers.

A trial that compares one period with another needs everything else held still, and one person sending one to three
messages a day never gives it enough. So the evidence is kept per contact: its time, what it was (a finding, a thought,
a question, a reminder, a nudge), how long, how many things it gathered, which of the day it was, whether the owner had
just been writing. And how it landed: an answer and how soon, reactions, and "too much", quiet or focus soon after.

From that the code sums up, as evidence and not as targets:
- how each kind of contact lands, and at which times, lengths and counts a day;
- the desk's errors both ways: what it held back that the owner then wanted (fetched from the shelf, "why didn't you
  tell me"), and what it sent that the owner didn't want;
- what the office has never tried, because a habit confirms itself: what the owner does answers what Audax already does.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.assumptions import ANSWERED, GAP
from agent.db import Database

UTC = ZoneInfo("UTC")
FEW = 3                       # fewer contacts than this in a group say nothing yet
ACTIVE = timedelta(minutes=45)
NEGATIVE = ("negative:amount", "negative:timing")
KINDS = {"opportunity": "fynd", "event": "fynd", "news": "fynd", "tool": "fynd", "social": "fynd", "job": "fynd",
         "insight": "tanke", "search_update": "tanke", "manager_note": "tanke", "thought": "tanke", "nudge": "knuff",
         "step": "steg"}


def _at(s: str) -> datetime:
    return datetime.fromisoformat(s[:19]).replace(tzinfo=UTC)


def _sql(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


async def contacts(db: Database, tz: str, days: int = 21, now: datetime | None = None) -> list[dict]:
    """The office's own contacts in the window, each with its context and how it landed."""
    zone = ZoneInfo(tz)
    now = now or datetime.now(zone)
    since = now - timedelta(days=days)
    msgs = [dict(r) for r in await db.fetchall(
        "SELECT id, direction, origin, created_at, length(text) AS n FROM messages WHERE created_at >= ? ORDER BY id", (_sql(since),))]
    fb = [dict(r) for r in await db.fetchall("SELECT message_id, signal, value, created_at FROM feedback WHERE created_at >= ?", (_sql(since),))]
    out: list[dict] = []
    for m in msgs:
        if m["direction"] != "out" or m["origin"] != "proactive":
            continue
        if out and _at(m["created_at"]) - _at(out[-1]["last"]) <= GAP:   # a long one sent in parts
            out[-1]["ids"].append(m["id"])
            out[-1]["length"] += m["n"] or 0
            out[-1]["last"] = m["created_at"]
            continue
        out.append({"ids": [m["id"]], "at": m["created_at"], "last": m["created_at"], "length": m["n"] or 0})
    owner = [m for m in msgs if m["direction"] == "in"]
    per_day: dict[str, int] = {}
    for c in out:
        at = _at(c["at"])
        local = at.astimezone(zone)
        marks = ",".join("?" * len(c["ids"]))
        kinds = [r["kind"] for r in await db.fetchall(
            f"SELECT c.kind FROM message_candidates mc JOIN candidates c ON c.id = mc.candidate_id WHERE mc.message_id IN ({marks})", c["ids"])]
        refs = [r["ref"] for r in await db.fetchall(f"SELECT ref FROM message_refs WHERE message_id IN ({marks})", c["ids"])]
        if kinds:
            kind = KINDS.get(kinds[0], "fynd")
        elif any(r.startswith(("#Q", "#P")) for r in refs):
            kind = "fråga"
        elif any(r.startswith(("#B", "#T")) for r in refs):
            kind = "påminnelse"
        else:
            kind = "annat"
        day = f"{local:%Y-%m-%d}"
        per_day[day] = per_day.get(day, 0) + 1
        nxt = next((o for o in owner if _at(o["created_at"]) > _at(c["last"])), None)
        answered = nxt is not None and _at(nxt["created_at"]) - _at(c["last"]) <= ANSWERED
        mine = [f for f in fb if f["message_id"] in c["ids"]]
        after = [f for f in fb if timedelta(0) <= _at(f["created_at"]) - at <= timedelta(hours=24)
                 and (f["value"] in NEGATIVE or (f["signal"] == "command" and f["value"][:5] in ("quiet", "focus")))]
        c.update({
            "local": local, "hour": local.hour, "weekday": local.weekday(), "kind": kind, "items": len(kinds),
            "nth": per_day[day],
            "owner_active": any(timedelta(0) < at - _at(o["created_at"]) <= ACTIVE for o in owner),
            "answered": answered,
            "minutes": round((_at(nxt["created_at"]) - _at(c["last"])).total_seconds() / 60) if answered else None,
            "reactions": [f["value"] for f in mine if f["signal"] == "reaction"],
            "too_much": bool(after),
        })
    for c in out:
        c["of_day"] = per_day[f"{c['local']:%Y-%m-%d}"]
    return out


NO_DATA = ("inget underlag om hur ägaren tar emot kontakt. Utgångsläge tills det finns: de flesta läser notiser när det "
           "passar och svarar långt ifrån alltid, så utebliven reaktion säger lite; ett svar säger mer.")


def baseline(cs: list[dict], days: int, recent: int = 7) -> str:
    """The latest week against the owner's own rate: three unanswered say something only against how they usually answer."""
    cut = max(c["local"] for c in cs) - timedelta(days=recent)
    new, old = [c for c in cs if c["local"] > cut], [c for c in cs if c["local"] <= cut]
    if not new or len(old) < FEW:
        return ""
    rate = lambda g: 100 * sum(c["answered"] for c in g) // len(g)  # noqa: E731
    return (f"- Senaste {recent} dagarna mot ägarens egen baslinje: svar på {sum(c['answered'] for c in new)} av {len(new)} "
            f"({rate(new)} %), mot {rate(old)} % av {len(old)} dessförinnan. Few contacts weigh little either way.")


def _rate(group: list[dict]) -> str:
    if len(group) < FEW:
        return f"{len(group)} (för få)"
    ans = sum(c["answered"] for c in group)
    much = sum(c["too_much"] for c in group)
    return f"{len(group)}, {100 * ans // len(group)} % svar" + (f", {much} 'för mycket'" if much else "")


def _bucket(hour: int) -> str:
    return "morgon" if hour < 10 else "dag" if hour < 17 else "kväll"


async def errors(db: Database, days: int = 14) -> dict:
    """The desk's errors both ways."""
    since = f"-{days} days"
    pulled = (await db.fetchone("SELECT count(*) AS n FROM feedback WHERE signal = 'shelf_pull' AND created_at >= datetime('now', ?)", (since,)))["n"]
    asked_why = (await db.fetchone("SELECT count(*) AS n FROM feedback WHERE value LIKE 'missed%' AND created_at >= datetime('now', ?)", (since,)))["n"]
    much = (await db.fetchone(
        f"SELECT count(*) AS n FROM feedback WHERE created_at >= datetime('now', ?) AND (value IN ({','.join('?' * len(NEGATIVE))}) "
        "OR (signal = 'command' AND (value LIKE 'quiet%' OR value LIKE 'focus%')) OR (signal = 'reaction' AND value = '👎'))",
        (since, *NEGATIVE)))["n"]
    from agent.timing import LET_PASS

    let_pass = (await db.fetchone(f"SELECT count(*) AS n FROM candidates c WHERE {LET_PASS} AND datetime(c.latest_useful) >= datetime('now', ?)",
                                  (since,)))["n"]
    return {"held_back": pulled + asked_why + let_pass, "pulled": pulled, "asked_why": asked_why, "let_pass": let_pass, "too_much": much}


async def summary(db: Database, tz: str, days: int = 21) -> str:
    """For Manager: how the office's own contacts land, the desk's errors both ways, and what was never tried."""
    cs = await contacts(db, tz, days)
    e = await errors(db)
    lines = [f"Kontaktliggaren, senaste {days} dagarna (evidence, not targets; a group under {FEW} says nothing yet):"]
    if not cs:
        lines.append(f"- Inga egna kontakter: {NO_DATA}")
    else:
        mins = sorted(c["minutes"] for c in cs if c["minutes"] is not None)
        lines.append(f"- Egna kontakter: {len(cs)} ({len(cs) / days:.1f}/dag), svar på {sum(c['answered'] for c in cs)}"
                     + (f", median efter {mins[len(mins) // 2]} min" if mins else "") + ".")
        for title, key in (("Per sort", lambda c: c["kind"]), ("Per tid", lambda c: _bucket(c["hour"])),
                           ("Per kontakt i ordningen den dagen", lambda c: str(min(c["nth"], 3)) + ("+" if c["nth"] >= 3 else "")),
                           ("Per längd", lambda c: "kort (<300)" if c["length"] < 300 else "mellan (300–700)" if c["length"] <= 700 else "lång (>700)"),
                           ("Samlade flera saker", lambda c: "ja" if c["items"] > 1 else "nej")):
            groups: dict[str, list] = {}
            for c in cs:
                groups.setdefault(key(c), []).append(c)
            lines.append(f"- {title}: " + "; ".join(f"{k}: {_rate(v)}" for k, v in sorted(groups.items())))
    if cs and (base := baseline(cs, days)):
        lines.append(base)
    lines.append(f"- Deskens fel, 14 dagar: höll tillbaka men ägaren ville ha det {e['held_back']} gånger (hämtat från hyllan "
                 f"{e['pulled']}, frågade varför inget sades {e['asked_why']}, lät tiden gå {e['let_pass']}: hölls tills ägaren inte längre "
                 f"hann använda det); skickade men ägaren ville inte {e['too_much']} gånger "
                 "('för mycket', tyst, bara det viktiga, 👎).")
    if never := await never_tried(cs, days):
        lines.append(f"- Aldrig prövat ({days} dagar): " + "; ".join(never) + ". A habit confirms itself; this is where nothing is known.")
    return "\n".join(lines)


async def never_tried(cs: list[dict], days: int) -> list[str]:
    if not cs:
        return ["egna kontakter över huvud taget"]
    out = []
    most = max(c["of_day"] for c in cs)
    out.append(f"fler än {most} egna kontakter en dag")
    hours = [c["hour"] for c in cs]
    if max(hours) < 18:
        out.append("en kontakt efter kl 18")
    if min(hours) >= 9:
        out.append("en kontakt före kl 9")
    if max(c["items"] for c in cs) <= 1:
        out.append("ett meddelande som samlar flera saker")
    kinds = {c["kind"] for c in cs}
    missing = [k for k in ("fynd", "tanke", "fråga") if k not in kinds]
    if missing:
        out.append("kontakter av sorten " + ", ".join(missing))
    return out


async def for_desk(db: Database, tz: str) -> str:
    """The desk's short version: today, how contacts have landed lately, and its own errors both ways."""
    cs = await contacts(db, tz, 14)
    e = await errors(db)
    today = datetime.now(ZoneInfo(tz)).date()
    n_today = sum(1 for c in cs if c["local"].date() == today)
    line = f"Own contacts today: {n_today}. Last 14 days: {len(cs)}"
    if cs:
        line += f", answered {sum(c['answered'] for c in cs)}, 'too much' after {sum(c['too_much'] for c in cs)}"
    else:
        line += f" ({NO_DATA})"
    line += (f". Your errors in 14 days: held back what the owner then wanted {e['held_back']} times (fetched from the shelf "
             f"{e['pulled']}, let the moment they had to know by pass {e['let_pass']}); sent what they didn't want {e['too_much']} times.")
    return line
