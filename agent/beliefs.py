"""Beliefs (instance/granskning/2026-10-20-omgang-2-utforskarna.md): what Audax holds about the owner, each with its
source, how sure, and how long it holds. A new one replaces an older one by superseding it; the old one stays in
the history, so a changed mind is visible as what changed, when and why, not as a patch in a text.

The profile is a view of them (omgång 5): written by the code from the current beliefs, grouped as the owner knows
it, every line with where it came from. No role edits it. It is what the roles read as "the profile" and what the
owner sees as "what Audax knows about me".
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent.db import Database
from agent.items import Items

HEADINGS = {
    "who": "Vem",
    "compensate": "Vad agenten ska kompensera för",
    "social": "Socialt och samarbete",
    "limits": "Gränser för agenten",
    "school": "Skola",
    "interests": "Intressen",
    "opportunities": "Typer av möjligheter",
    "not_interested": "Inte intresserad av",
    "interrupt": "Får alltid störa",
    "schedule": "Schema och vanor",
    "communication": "Hur hen vill ha det",
    "name": "Vad agenten och rollerna heter",
    "other": "Övrigt",
}
IN_PROFILE = ("preference", "fact", "value")   # patterns and hypotheses are Director's interpretation, not the profile
MONTHS = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
HEADER = ("# Profil\n\n<!-- Skrivs av koden ur uppfattningarna (#U) och ändras inte här. Det du säger blir en uppfattning med "
          "källa; en ny ersätter en äldre, och den gamla finns kvar i historiken. -->\n")


async def current(db: Database, area: str | None = None, subject: str | None = None, today: date | None = None,
                  kinds: tuple[str, ...] | None = None) -> list[dict]:
    """The beliefs that hold now: not superseded or retracted, and not past their date."""
    today = today or date.today()
    where, args = ["(valid_until IS NULL OR substr(valid_until, 1, 10) >= ?)"], [today.isoformat()]
    if area:
        where.append("json_extract(data, '$.area') = ?"); args.append(area)
    if subject:
        where.append("lower(json_extract(data, '$.subject')) = lower(?)"); args.append(subject)
    return await Items(db).find("belief", kind=kinds, status="current", where=" AND ".join(where), args=tuple(args), order="id")


def source_note(item: dict, owner_name: str = "ägaren") -> str:
    """Where a belief came from, in a few words: 'Alex, 9 okt', 'sett 14 okt', 'slutsats, Director'."""
    src = item.get("source") or {}
    when = src.get("date") or (item.get("created_at") or "")[:10]
    try:
        d = date.fromisoformat(when[:10])
        when = f"{d.day} {MONTHS[d.month - 1]}"
    except ValueError:
        when = ""
    kind = src.get("type")
    if kind == "owner":
        return f"{owner_name}{', ' + when if when else ''}"
    if kind == "observed":
        return f"sett{' ' + when if when else ''}"
    if kind == "external":
        return f"enligt {src.get('url') or src.get('by') or 'en källa'}"
    return f"slutsats, {(src.get('by') or item.get('owner') or 'kontoret').capitalize()}"


def weight(item: dict, owner_name: str = "ägaren") -> str:
    """How much a belief rests on, in a few words, so a conclusion never looks stronger than it is (omgång 13): the
    owner's own words, or the observations it cites (how many, over how long, the latest), or nothing at all."""
    src = item.get("source") or {}
    if src.get("type") == "owner":
        return f"{owner_name}s egna ord"
    if src.get("type") == "assumed":
        return "ett antagande, aldrig bekräftat"
    dates = []
    lines = [x.strip() for x in (item.get("observations") or "").splitlines() if x.strip()]
    for line in lines:
        try:
            dates.append(date.fromisoformat(line[:10]))
        except ValueError:
            pass
    if not lines:
        return "inget belägg angivet" + (" utom en fritext" if src.get("evidence") else "")
    span = ""
    if dates:
        a, b = min(dates), max(dates)
        span = f", {a.day} {MONTHS[a.month - 1]}" + (f"–{b.day} {MONTHS[b.month - 1]}" if b != a else "")
    return f"vilar på {len(lines)} observation{'er' if len(lines) != 1 else ''}{span}"


PRACTICE_HEADER = ("# Kontorets praxis\n\n<!-- Skrivs av koden ur uppfattningarna av sorten practice (#U): hur kontoret arbetar, "
                   "med vad varje sak vilar på. Ändras genom uppfattningarna, inte här. -->\n")


async def practice_text(db: Database) -> str:
    rows = await current(db, kinds=("practice",))
    lines = [PRACTICE_HEADER]
    for b in rows:
        lines.append(f"- {b['title']} ({(b.get('owner') or 'kontoret').capitalize()}; {weight(b)}) #U{b['id']}")
    return "\n".join(lines).rstrip() + "\n"


async def write_practice(db: Database, memory: Path) -> bool:
    """lardomar.md is a view of the practice beliefs, as profile.md is of the others. Returns whether it changed."""
    path = memory / "lardomar.md"
    await move_lessons(db, memory)
    text = await practice_text(db)
    if path.exists() and path.read_text() == text:
        return False
    path.write_text(text)
    return True


async def move_lessons(db: Database, memory: Path) -> int:
    """Once: the lessons written as lines in lardomar.md become practice beliefs, word for word, with the role and the
    date they carried. A line that already refers to a belief is the view itself and is left."""
    import re

    path = memory / "lardomar.md"
    if not path.exists() or path.read_text().startswith("# Kontorets praxis"):
        return 0
    old = path.read_text()
    path.write_text(PRACTICE_HEADER)   # first: each belief made below rewrites this view, which must not move them again (9 Nov)
    moved = 0
    for line in old.splitlines():
        m = re.match(r"- (.+?)\s*\((Assistant|Director|Researcher|Manager), (\d+) (\w+)\)\s*$", line.strip())
        if not m:
            continue
        text, role, day, month = m.groups()
        text = re.sub(r"\s*\((Assistant|Director|Researcher|Manager), \d+ \w+\)\.?$", "", text).strip()
        when = date.today()
        if month in MONTHS:
            when = date(when.year, MONTHS.index(month) + 1, int(day))
        await Items(db).create("belief", text, "system", kind="practice", owner=role.lower(),
                               data={"observations": f"{when.isoformat()}: lärdom skriven av {role} i lardomar.md"},
                               source={"type": "inferred", "by": role.lower(), "date": when.isoformat()},
                               note="flyttad från lardomar.md (omgång 13)")
        moved += 1
    return moved


async def add_practice(db: Database, memory: Path, role: str, lessons: list[str], when) -> list[str]:
    """A role's lessons are practice beliefs: what it rests on is today's run, and more can be added later."""
    out = []
    for x in [x.strip() for x in lessons or [] if x and x.strip()]:
        await Items(db).create("belief", x, role, kind="practice", owner=role,
                               data={"observations": f"{when:%Y-%m-%d}: {role}s körning"},
                               source={"type": "inferred", "by": role, "date": f"{when:%Y-%m-%d}"})
        out.append(x)
    if out:
        await write_practice(db, memory)
    return out


async def profile_text(db: Database, owner_name: str = "ägaren", tz: str = "Europe/Stockholm") -> str:
    today = datetime.now(ZoneInfo(tz)).date()
    beliefs = await current(db, today=today, kinds=IN_PROFILE)
    by_area: dict[str, list[dict]] = {}
    for b in beliefs:
        by_area.setdefault(b.get("area") or "other", []).append(b)
    parts = [HEADER]
    for area, heading in HEADINGS.items():
        rows = by_area.get(area)
        if not rows:
            continue
        parts.append(f"## {heading}")
        for b in rows:
            extra = [source_note(b, owner_name)]
            if b.get("scope"):
                extra.append(b["scope"])
            if b.get("valid_until"):
                extra.append(f"gäller till {b['valid_until'][:10]}")
            if b.get("confidence") == "low":
                extra.append("osäkert")
            if b.get("bubble") == "maybe_limit":
                extra.append("kanske en begränsning snarare än en preferens")
            if (b.get("source") or {}).get("type") != "owner":
                extra.append(weight(b, owner_name))
            parts.append(f"- {b['title']} ({'; '.join(extra)}) #U{b['id']}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


async def write_profile(db: Database, memory: Path, owner_name: str = "ägaren", tz: str = "Europe/Stockholm") -> bool:
    """Writes memory/profile.md from the beliefs when there are any. Returns whether the file changed."""
    if not await Items(db).find("belief", limit=1):
        return False   # nothing moved over yet: leave the profile as it is
    text = await profile_text(db, owner_name, tz)
    path = memory / "profile.md"
    if path.exists() and path.read_text() == text:
        return False
    path.write_text(text)
    return True
