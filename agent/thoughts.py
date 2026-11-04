"""The office's thoughts for the owner (instance/granskning/2026-11-04-omgang-11-kontakten.md, stage 2).

What the roles understand, notice and plan stayed in the log: Director's ideas were "usually none" and only in the
reflection, Researcher's word to the owner "rarely", Manager's note almost never; seven reached Julius in a month.
Now Director, Researcher and Manager may each put a thought for the owner: a pattern they noticed, an idea, a plan
that has taken shape, what they are working on, a hypothesis about what the owner wants. Sometimes it carries a direct
question, to learn something ("Vill du ha fler sådana här?"), never a hypothetical ("säg till om …").

The bar is the findings' bar: would the owner be glad to hear it, even if nothing comes of it? The desk weighs a thought
like everything else, the Assistant writes it in the one voice, and the ledger shows how thoughts land; each role sees
how its own landed.
"""

from __future__ import annotations

from agent.db import Database
from agent.pipeline.outbox import add_candidate

KIND = "thought"
SHOWN = 10

SCHEMA = {
    "type": "array",
    "description": "Thoughts for the owner: something you noticed, an idea, a plan that has taken shape, what you are "
                   "working on, a hypothesis about what they want. Only what they would be glad to hear even if nothing "
                   "comes of it; never filler. Often none, sometimes one or two.",
    "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["pattern", "idea", "plan", "working_on", "hypothesis"]},
            "title": {"type": "string"},
            "thought": {"type": "string", "description": "Swedish: the thought itself, with what it rests on; the Assistant writes the message from it"},
            "why_them": {"type": "string", "description": "Swedish, one sentence: why the owner would be glad to hear it; shown if they ask why"},
            "question": {"type": ["string", "null"], "description": "Swedish: a direct question it carries to learn something, or null"},
            "url": {"type": ["string", "null"]},
            "time_sensitivity": {"type": "string", "enum": ["low", "medium", "high"]},
            "expires": {"type": ["string", "null"], "description": "YYYY-MM-DD if it has a best-before date"},
        },
        "required": ["kind", "title", "thought", "why_them", "time_sensitivity"],
    },
}

WORDS = {"pattern": "något jag märkt", "idea": "en idé", "plan": "en plan", "working_on": "något jag håller på med",
         "hypothesis": "en gissning om vad du vill"}


async def add(db: Database, role: str, thoughts: list | None, tz: str) -> list[str]:
    """The role's thoughts join the outbox; the desk decides whether and when."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from agent.schedule import utc_iso

    out = []
    for t in (thoughts or [])[:3]:
        if not (t.get("thought") or "").strip():
            continue
        expires = None
        if t.get("expires"):
            try:
                expires = utc_iso(datetime.fromisoformat(t["expires"][:10]).replace(hour=23, minute=59, tzinfo=ZoneInfo(tz)))
            except ValueError:
                pass
        body = t["thought"].strip() + (f"\nFråga att ställa rakt ut: {t['question'].strip()}" if (t.get("question") or "").strip() else "")
        await add_candidate(
            db, kind=KIND, title=t["title"], body=body, url=t.get("url"),
            features={"relevance": "high", "time_sensitivity": t.get("time_sensitivity", "low"), "thought": t["kind"],
                      "from": role, "asks": bool((t.get("question") or "").strip()),
                      "reason": f"{role.capitalize()}, {WORDS.get(t['kind'], 'en tanke')}: {t.get('why_them', '')}"},
            expires_at=expires)
        out.append(f"tanke till ägaren: {t['title']}")
    return out


async def outcomes(db: Database, role: str) -> str:
    """For a role's review: how its own thoughts landed."""
    rows = await db.fetchall(
        "SELECT c.title, c.status, c.features, (SELECT group_concat(f.signal || ':' || f.value, ', ') FROM feedback f "
        "WHERE f.candidate_id = c.id OR f.message_id IN (SELECT message_id FROM message_candidates WHERE candidate_id = c.id)) AS fb, "
        "(SELECT 1 FROM message_candidates mc JOIN messages m ON m.id = mc.message_id JOIN messages a ON a.direction = 'in' "
        " AND a.id > m.id AND a.created_at <= datetime(m.created_at, '+12 hours') WHERE mc.candidate_id = c.id LIMIT 1) AS answered "
        "FROM candidates c WHERE c.kind IN ('thought', 'insight', 'search_update', 'manager_note') "
        "AND json_extract(c.features, '$.from') = ? ORDER BY c.id DESC LIMIT ?", (role, SHOWN))
    if not rows:
        return ""
    lines = [f"- {r['title']}: {r['status']}" + (", besvarad" if r["answered"] else "") + (f", {r['fb']}" if r["fb"] else "")
             for r in rows]
    return "Dina tankar till ägaren och hur de landade (sent, held, saved, dropped; answered or not):\n" + "\n".join(lines)
