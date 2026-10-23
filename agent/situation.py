"""The situation (instance/granskning/2026-10-20-omgang-4-rosten.md, "Vad chatten vet"): what is actually going on
behind the chat, read rather than remembered. The same data the office page shows, in a few lines: what each role
did last and has waiting, the steps with their dates, what waits for the budget and why, what was booked, and what
the owner was sent today. The chat says nothing about the background it hasn't read here (19 Oct: "börjar inom en
minut", "senast vi sökte var 12 okt", neither true).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from agent.db import Database
from agent.items import Items, ref
from agent.vocab import NAMES, ROLES, kinds_of, task_name


def _local(sqlite_utc: str | None, tz: ZoneInfo) -> str:
    if not sqlite_utc:
        return "aldrig"
    t = datetime.fromisoformat(sqlite_utc[:19] + "+00:00").astimezone(tz)
    return f"{t:%a %d/%m %H:%M}"


FAST = {"triage", "dispatcher", "page_extract", "research", "research_deep", "deliver", "chat_summary", "scout_queries"}


async def _last(db: Database, kinds: list[str]):
    if not kinds:
        return None
    return await db.fetchone(
        f"SELECT kind, finished_at, summary, is_error FROM claude_runs WHERE kind IN ({','.join('?' * len(kinds))}) "
        "AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", tuple(kinds))


async def roles(db: Database, tz: ZoneInfo) -> list[str]:
    """Per role its latest slow run (review, planning, night pass) and, apart, its latest fast skill: the latest run
    alone was usually a triage or a page read, and hid the night's work (23 Oct: "Director har hängt sig")."""
    lines = []
    for role in ROLES:
        kinds = [k for k in kinds_of(role) if k != "chat"]
        slow, fast = await _last(db, [k for k in kinds if k not in FAST]), await _last(db, [k for k in kinds if k in FAST])
        parts = []
        if slow:
            what = f"senast {task_name(slow['kind']).lower()} {_local(slow['finished_at'], tz)}" + (" (misslyckades)" if slow["is_error"] else "")
            if slow["summary"]:
                what += f": {' '.join(slow['summary'].split())[:220]}"
            parts.append(what)
        if fast:
            parts.append(f"snabbt arbete senast: {task_name(fast['kind']).lower()} {_local(fast['finished_at'], tz)}"
                         + (" (misslyckades)" if fast["is_error"] else ""))
        waiting = await db.fetchone("SELECT count(*) AS n FROM posts WHERE recipient = ? AND status = 'new'", (role,))
        lines.append(f"- {NAMES[role]}: {'; '.join(parts) or 'inget eget arbete ännu'}" + (f" · {waiting['n']} i inkorgen" if waiting["n"] else ""))
    return lines


async def plans(db: Database, limit: int = 15) -> list[str]:
    items = Items(db)
    out = []
    for s in await items.find("step", open_only=True, order="review_at, id", limit=limit):
        parent = await items.get(s["parent_id"]) if s["parent_id"] else None
        out.append(f"- {ref(s)} {s['owner'] or '?'}: {s['title']} · {s['status']} · {s['review_at'] or 'utan datum'}"
                   + (f" · efter {s['after']}" if s.get("after") else "")
                   + (f" · för {ref(parent)} {parent['title']}" if parent else ""))
    for e in await items.find("experiment", status=("planned", "running", "evaluating"), limit=6):
        out.append(f"- {ref(e)} {e['owner']}: experiment {e['title']} · {e['status']} · bedöms {e['review_at'] or '?'}")
    return out


async def sent_today(db: Database, tz: ZoneInfo) -> list[str]:
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    rows = await db.fetchall("SELECT created_at, text FROM messages WHERE direction = 'out' AND origin = 'proactive' AND created_at >= ? ORDER BY id",
                             (f"{start:%Y-%m-%d %H:%M:%S}",))
    return [f"- {_local(r['created_at'], tz)[-5:]} {' '.join(r['text'].split())[:100]}" for r in rows]


async def situation(db: Database, tz: str, budget=None, agenda=None) -> str:
    """Now, in Swedish lines for the chat: the roles, the plans, what waits, what is booked, what was sent."""
    z = ZoneInfo(tz)
    parts = ["Rollerna (det senaste var och en gjorde):\n" + "\n".join(await roles(db, z))]
    if p := await plans(db):
        parts.append("Planerat (steg med datum och vem som gör dem; experiment som pågår):\n" + "\n".join(p))
    else:
        parts.append("Planerat: inga öppna steg.")
    questions = await Items(db).find("question", status=("open", "asked"), limit=8)
    if questions:
        parts.append("Frågor som väntar på svar:\n" + "\n".join(f"- {ref(q)} {q['owner']}: {q['title']} · {q['status']}" for q in questions))
    if budget is not None:
        parts.append(await budget.words())
    if agenda is not None:
        parts.append("Bokat de närmaste tre dagarna:\n" + await agenda.upcoming(3))
    sent = await sent_today(db, z)
    parts.append("Skickat till ägaren på eget initiativ i dag:\n" + "\n".join(sent) if sent else "Skickat på eget initiativ i dag: inget.")
    return "\n\n".join(parts)
