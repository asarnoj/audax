"""What is watched works (instance/granskning/2026-11-11-omgang-15-sokningen-larer.md, stage 1).

A watch with many sources in one (today the company watchlist: one sense reading each company's applicant tracking
system) failed per entry in silence: an entry whose address went 404 was a line in the log, and the watch itself looked
healthy. So each entry has its own health: when it was last read without error, and since when it fails. An entry that
starts failing is a point for Researcher to decide (agent/docket.py): fix the address or strike the entry. When it
reads again the point resolves itself. A watch that fails as a whole already pauses itself and shows in the review with
its error (agent/pipeline/ingest.py).

That the missions' own texts age with the owner's words (stage 1 B) is now part of the general aging (agent/points.py).
"""

from __future__ import annotations

from agent import docket
from agent.db import Database

ENTRY_RESULTS: dict[str, dict[str, str | None]] = {}   # stream id -> entry -> None (read fine) or the error, from the last poll


async def record_entries(db: Database, stream_id: str) -> None:
    """After a poll: each entry's health (called by the ingest when the sense as a whole was read)."""
    for entry, error in (ENTRY_RESULTS.pop(stream_id, None) or {}).items():
        key = f"{stream_id}/{entry}"
        if error is None:
            await db.execute("INSERT INTO watch_health (key, stream, entry, last_ok) VALUES (?, ?, ?, datetime('now')) "
                             "ON CONFLICT(key) DO UPDATE SET last_ok = datetime('now'), failing_since = NULL, error = NULL",
                             (key, stream_id, entry))
            await docket.resolve(db, f"failing:{key}", "läses igen utan fel")
        else:
            first = error.splitlines()[0][:200] if error.strip() else "fel utan text"
            await db.execute("INSERT INTO watch_health (key, stream, entry, failing_since, error) VALUES (?, ?, ?, datetime('now'), ?) "
                             "ON CONFLICT(key) DO UPDATE SET failing_since = COALESCE(failing_since, datetime('now')), error = excluded.error",
                             (key, stream_id, entry, first))
            row = await db.fetchone("SELECT * FROM watch_health WHERE key = ?", (key,))
            ok = f"senast läst {row['last_ok'][:10]}" if row["last_ok"] else "aldrig läst"
            await docket.raise_point(
                db, "researcher", f"failing:{key}", "failing", f"{entry} ({stream_id}) går inte att läsa",
                f"Fel: {first}. Sedan {row['failing_since'][:10]}, {ok}. It is unwatched while it fails: fix its address "
                "(remove_company, then add_company with the right careers page) or strike it (remove_company) with a reason.",
                weight=2, quiet_days=3)


async def failing(db: Database) -> list:
    return await db.fetchall("SELECT * FROM watch_health WHERE failing_since IS NOT NULL ORDER BY failing_since")
