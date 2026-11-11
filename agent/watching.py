"""What is watched works, and what was written before the owner's words changed is looked at again
(instance/granskning/2026-11-11-omgang-15-sokningen-larer.md, stage 1).

A. A watch with many sources in one (today the company watchlist: one sense reading each company's applicant tracking
system) failed per entry in silence: an entry whose address went 404 was a line in the log, and the watch itself looked
healthy. So each entry has its own health: when it was last read without error, and since when it fails. Researcher's
review shows what fails, and Researcher fixes the address or strikes the entry. A watch that fails as a whole already
pauses itself and shows in the review with its error (agent/pipeline/ingest.py).

B. A judgement is as old as what it was judged against (agent/pipeline/rejudge.py), and that holds for the search's own
texts too: a mission's goal and queries written before the owner's own words changed may search for what they no
longer want. The code compares when each mission was last written with when the owner's words changed; Researcher sees
it once, beside the words that changed, and keeps, rewrites or strikes.
"""

from __future__ import annotations

from datetime import datetime

from agent.db import Database

ENTRY_RESULTS: dict[str, dict[str, str | None]] = {}   # stream id -> entry -> None (read fine) or the error, from the last poll
LOOKED_KEY = "watching.looked.{id}"   # a mission -> the change in the owner's words it was last shown beside
SHOWN_BELIEFS = 8


async def record_entries(db: Database, stream_id: str) -> None:
    """After a poll: each entry's health (called by the ingest when the sense as a whole was read)."""
    for entry, error in (ENTRY_RESULTS.pop(stream_id, None) or {}).items():
        key = f"{stream_id}/{entry}"
        if error is None:
            await db.execute("INSERT INTO watch_health (key, stream, entry, last_ok) VALUES (?, ?, ?, datetime('now')) "
                             "ON CONFLICT(key) DO UPDATE SET last_ok = datetime('now'), failing_since = NULL, error = NULL",
                             (key, stream_id, entry))
        else:
            await db.execute("INSERT INTO watch_health (key, stream, entry, failing_since, error) VALUES (?, ?, ?, datetime('now'), ?) "
                             "ON CONFLICT(key) DO UPDATE SET failing_since = COALESCE(failing_since, datetime('now')), error = excluded.error",
                             (key, stream_id, entry, error[:200]))


async def failing(db: Database) -> list:
    return await db.fetchall("SELECT * FROM watch_health WHERE failing_since IS NOT NULL ORDER BY failing_since")


async def entries_for_review(db: Database, tz) -> str:
    """`tz`: the owner's time zone (a tzinfo)."""
    rows = await failing(db)
    if not rows:
        return ""
    lines = ["Bevakade poster som inte går att läsa (each is unwatched while it fails: fix its address with remove_company "
             "and then add_company with the right careers page, or only remove_company with a reason):"]
    for r in rows:
        since = datetime.fromisoformat(r["failing_since"] + "+00:00").astimezone(tz)
        ok = f", senast läst {r['last_ok'][:10]}" if r["last_ok"] else ", aldrig läst"
        lines.append(f"- {r['entry']} ({r['stream']}): {r['error']}, sedan {since:%d %b}{ok}")
    return "\n".join(lines)


async def owner_words_changed(db: Database) -> str | None:
    r = await db.fetchone("SELECT max(updated_at) AS t FROM items WHERE form = 'belief' AND json_extract(source, '$.type') = 'owner'")
    return r["t"] if r else None


async def aged_missions(db: Database) -> list[tuple[dict, list]]:
    """Active missions last written before the owner's own words changed, not yet shown beside that change."""
    from agent import routines

    changed = await owner_words_changed(db)
    if not changed:
        return []
    out = []
    for r in await routines.all_routines(db, "mission"):
        if r["status"] != "active" or r["updated_at"] >= changed:
            continue
        if await db.kv_get(LOOKED_KEY.format(id=r["id"])) == changed:
            continue
        since = await db.fetchall(
            "SELECT id, title, status FROM items WHERE form = 'belief' AND json_extract(source, '$.type') = 'owner' AND updated_at > ? "
            "ORDER BY updated_at DESC LIMIT ?", (r["updated_at"], SHOWN_BELIEFS))
        out.append((r, since))
    return out


async def missions_for_review(db: Database) -> str:
    aged = await aged_missions(db)
    if not aged:
        return ""
    lines = ["Uppdrag skrivna innan ägarens egna ord ändrades (does the goal or a query still search for what they want? "
             "keep it, rewrite it or strike a query; shown once per change):"]
    for r, beliefs in aged:
        mission = r["_config"]["mission"]
        lines.append(f"- {mission['id']} (senast skrivet {r['updated_at'][:10]}): mål: {mission.get('goal', '')[:240]}")
        lines.append("  frågor: " + " | ".join(q[:90] for q in mission.get("queries", [])))
        lines.append("  sedan dess: " + "; ".join(f"#U{b['id']} {b['title'][:90]}" + (" (struken)" if b["status"] != "current" else "")
                                              for b in beliefs))
    return "\n".join(lines)


async def mark_looked(db: Database) -> None:
    """After Researcher's run: what it was shown, it has looked at."""
    changed = await owner_words_changed(db)
    for r, _ in await aged_missions(db):
        await db.kv_set(LOOKED_KEY.format(id=r["id"]), changed)
