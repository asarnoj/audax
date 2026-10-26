"""IDs and lookup (docs/MODEL.md, "ID:n och uppslag"): structure instead of free text.

Everything worth following up has a short ID: #P posts, #S what the owner goes for (goals, projects, opportunities,
ideas), #F findings (outbox candidates) and #B bookings. Messages link to the IDs they are about, so the Assistant looks things up ("what did I promise about
Google?") instead of trying to remember. IDs are the kitchen's tools: they are for the office and the web page, not
for the owner's chat.
"""

from __future__ import annotations

import json
import re

from agent.db import Database
from agent.forms import LETTERS
from agent.items import Items
from agent.search import search, words_of

# Posts, findings and bookings have their own tables; every other letter is a form of the office's items (agent/forms.py).
PATTERN = re.compile(r"#?([PFB" + "".join(LETTERS) + r"])(\d+)\b", re.I)


async def link(db: Database, message_ids: list[int], refs: set[str]) -> None:
    for mid in message_ids:
        for r in refs:
            await db.execute("INSERT OR IGNORE INTO message_refs (message_id, ref) VALUES (?, ?)", (mid, r))
    from datetime import datetime

    from agent import threads

    await threads.mentioned(db, message_ids, refs, datetime.now())   # the owner's words about a followed thing are news in it


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
    if kind in LETTERS:
        item = await Items(db).get(n)
        if item is None or item["form"] != LETTERS[kind]:
            return None
        return await Items(db).describe(n)
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
    doubt = (json.loads(c["features"] or "{}").get("unconfirmed") or "").strip()
    return f"#F{n} · {c['kind']} · {c['status']} · {c['title']}\n{c['body']}" + (f"\n{c['url']}" if c["url"] else "") + (
        f"\nNOT confirmed: {doubt}" if doubt else "") + (f"\nDecision: {why['reason']}" if why else "")


async def lookup(db: Database, query: str, limit: int = 12) -> str:
    """An ID gives its details; words search the conversation, posts, findings and items (agent/search.py): best
    matches first, each with who said it and when."""
    ids = PATTERN.findall(query)
    if ids and len(query.strip()) <= 12 * len(ids):
        found = [await describe(db, f"{k}{n}") for k, n in ids]
        return "\n\n".join(f for f in found if f) or "No such ID."
    hits = await search(db, query, limit=limit)
    if not hits:
        return "Nothing found." if words_of(query) else "Give an ID (#P12, #S3, #F210, #B4) or a few words."
    lines = []
    for h in hits:
        tags = ""
        if h.kind == "message":
            row = await db.fetchone("SELECT group_concat(ref, ' ') AS refs FROM message_refs WHERE message_id = ?", (h.ref,))
            tags = f" [{row['refs']}]" if row and row["refs"] else ""
        lines.append(h.line() + tags)
    return "\n".join(lines)

