"""IDs and lookup (docs/MODEL.md, "ID:n och uppslag"): structure instead of free text.

Everything worth following up has a short ID: #P posts, #S pursuits, #F findings (outbox candidates) and #B
bookings. Messages link to the IDs they are about, so the Assistant looks things up ("what did I promise about
Google?") instead of trying to remember. IDs are the kitchen's tools: they are for the office and the web page, not
for the owner's chat.
"""

from __future__ import annotations

import re

from agent.db import Database

PATTERN = re.compile(r"#?([PSFB])(\d+)\b", re.I)


async def link(db: Database, message_ids: list[int], refs: set[str]) -> None:
    for mid in message_ids:
        for r in refs:
            await db.execute("INSERT OR IGNORE INTO message_refs (message_id, ref) VALUES (?, ?)", (mid, r))


async def describe(db: Database, ref: str) -> str | None:
    m = PATTERN.fullmatch(ref.strip())
    if not m:
        return None
    kind, n = m.group(1).upper(), int(m.group(2))
    if kind == "P":
        p = await db.fetchone("SELECT * FROM posts WHERE id = ?", (n,))
        if p is None:
            return None
        text = f"#P{n} · {p['created_at'][:16]} · {p['sender']} → {p['recipient']} · {p['kind']} · {p['status']}\n{p['body']}"
        text += f"\nThe owner's words: \"{p['verbatim']}\"" if p["verbatim"] else ""
        text += f"\nOutcome: {p['outcome']}" if p["outcome"] else ""
        return text
    if kind == "S":
        p = await db.fetchone("SELECT * FROM pursuits WHERE id = ?", (n,))
        if p is None:
            return None
        log = await db.fetchall("SELECT created_at, status, note, by FROM pursuit_log WHERE pursuit_id = ? ORDER BY id", (n,))
        return (
            f"#S{n} · {p['title']}" + (f" ({p['organization']})" if p["organization"] else "") + f" · {p['status']}"
            + (f" · deadline {p['deadline']}" if p["deadline"] else "") + (f" · reminder {p['remind_at']}" if p["remind_at"] else "")
            + (f"\nNext: {p['next_step']}" if p["next_step"] else "") + (f"\n{p['url']}" if p["url"] else "")
            + "\nHistory:\n" + "\n".join(f"- {r['created_at'][:10]} {r['status'] or ''} {r['note'] or ''} ({r['by']})" for r in log)
        )
    if kind == "B":
        b = await db.fetchone("SELECT * FROM bookings WHERE id = ?", (n,))
        if b is None:
            return None
        return (
            f"#B{n} · {b['kind']} · {b['status']} · due {b['due_at'][:16]} UTC" + (f" (latest {b['latest_at'][:16]})" if b["latest_at"] else "")
            + f" · by {b['requested_by']}" + (f" · about {b['ref']}" if b["ref"] else "") + f"\n{b['title']}\n{b['body']}"
            + (f"\n{b['reason']}" if b["reason"] else "")
        )
    c = await db.fetchone("SELECT * FROM candidates WHERE id = ?", (n,))
    if c is None:
        return None
    why = await db.fetchone("SELECT reason FROM decisions WHERE candidate_id = ? ORDER BY id DESC LIMIT 1", (n,))
    return f"#F{n} · {c['kind']} · {c['status']} · {c['title']}\n{c['body']}" + (f"\n{c['url']}" if c["url"] else "") + (
        f"\nDecision: {why['reason']}" if why else ""
    )


async def lookup(db: Database, query: str, limit: int = 12) -> str:
    """An ID gives its details; words search posts, pursuits, findings and the conversation, newest first."""
    ids = PATTERN.findall(query)
    if ids and len(query.strip()) <= 12 * len(ids):
        found = [await describe(db, f"{k}{n}") for k, n in ids]
        return "\n\n".join(f for f in found if f) or "No such ID."
    words = [w for w in re.findall(r"\w+", query.casefold()) if len(w) > 2]
    if not words:
        return "Give an ID (#P12, #S3, #F210, #B4) or a few words."

    def match(text: str | None) -> bool:
        return bool(text) and all(w in text.casefold() for w in words)

    hits: list[tuple[str, str]] = []
    for p in await db.fetchall("SELECT id, created_at, sender, recipient, kind, status, body, verbatim FROM posts ORDER BY id DESC LIMIT 400"):
        if match(f"{p['body']} {p['verbatim'] or ''}"):
            hits.append((p["created_at"], f"#P{p['id']} {p['created_at'][:10]} {p['sender']}→{p['recipient']} {p['kind']} ({p['status']}): {p['body'][:140]}"))
    for s in await db.fetchall("SELECT id, updated_at, title, organization, status, deadline, note FROM pursuits"):
        if match(f"{s['title']} {s['organization'] or ''} {s['note'] or ''}"):
            hits.append((s["updated_at"], f"#S{s['id']} {s['title']} · {s['status']}" + (f" · deadline {s['deadline']}" if s["deadline"] else "")))
    for c in await db.fetchall(
        "SELECT id, updated_at, title, body, status FROM candidates WHERE updated_at >= datetime('now', '-90 days') ORDER BY id DESC LIMIT 600"
    ):
        if match(f"{c['title']} {c['body']}"):
            hits.append((c["updated_at"], f"#F{c['id']} {c['title'][:100]} ({c['status']})"))
    for m in await db.fetchall(
        "SELECT m.id, m.created_at, m.direction, m.text, (SELECT group_concat(ref, ' ') FROM message_refs r WHERE r.message_id = m.id) AS refs "
        "FROM messages m WHERE m.created_at >= datetime('now', '-60 days') ORDER BY m.id DESC LIMIT 800"
    ):
        if match(m["text"]):
            who = "owner" if m["direction"] == "in" else "you"
            hits.append((m["created_at"], f"{m['created_at'][:16]} {who}: {m['text'][:160]}" + (f" [{m['refs']}]" if m["refs"] else "")))
    hits.sort(key=lambda h: h[0], reverse=True)
    return "\n".join(h for _, h in hits[:limit]) or "Nothing found."
