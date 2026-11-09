"""A belief's record (instance/granskning/2026-11-09-omgang-13-levande-kunskap.md, C).

When triage or the desk keeps, drops, holds or sends something leaning on a belief (a rule in the policy, a frame in
now.md, what the owner said), it names it, and the code follows what came of it: what was held back and then wanted
after all (fetched from the shelf, asked about), what was sent and answered or met with "too much". A belief whose
record points against it since it was last looked at comes up in its owner's review: keep it with a reason, change
it, or let it go. Nothing ends because time passed; it ends when what it rests on no longer holds.
"""

from __future__ import annotations

import re

from agent.db import Database

REF = re.compile(r"#?U(\d+)")
NEGATIVE = "('negative:amount', 'negative:timing', '👎')"


async def record(db: Database, refs, candidate_id: int, module: str, decision: str) -> None:
    for r in refs or []:
        if m := REF.fullmatch(str(r).strip()):
            if await db.fetchone("SELECT 1 FROM items WHERE id = ? AND form = 'belief'", (int(m.group(1)),)):
                await db.insert("belief_uses", belief_id=int(m.group(1)), candidate_id=candidate_id, module=module, decision=decision)


async def of(db: Database, belief_id: int, since: str | None = None) -> dict:
    """What the decisions leaning on a belief came to: held back and wanted after all; sent and answered or not wanted.
    With `since`, only what the owner did after it (wanted, not wanted) counts as wanted or unwanted."""
    when = "AND f.created_at > ?" if since else ""
    extra = (since,) if since else ()
    held = await db.fetchone(
        "SELECT count(DISTINCT u.candidate_id) AS n, count(DISTINCT CASE WHEN EXISTS (SELECT 1 FROM feedback f "
        f" WHERE f.candidate_id = c.id AND (f.signal = 'shelf_pull' OR f.value LIKE 'missed%') {when}) THEN c.id END) AS wanted "
        "FROM belief_uses u JOIN candidates c ON c.id = u.candidate_id WHERE u.belief_id = ? AND u.decision IN ('drop', 'shelf')",
        (*extra, belief_id))
    sent = await db.fetchone(
        "SELECT count(DISTINCT c.id) AS n, "
        "count(DISTINCT CASE WHEN EXISTS (SELECT 1 FROM message_candidates mc JOIN messages m ON m.id = mc.message_id "
        " JOIN messages a ON a.direction = 'in' AND a.id > m.id AND a.created_at <= datetime(m.created_at, '+12 hours') "
        " WHERE mc.candidate_id = c.id) THEN c.id END) AS answered, "
        "count(DISTINCT CASE WHEN EXISTS (SELECT 1 FROM feedback f WHERE (f.candidate_id = c.id OR f.message_id IN "
        f" (SELECT message_id FROM message_candidates WHERE candidate_id = c.id)) AND f.value IN {NEGATIVE} {when}) THEN c.id END) AS unwanted "
        "FROM belief_uses u JOIN candidates c ON c.id = u.candidate_id WHERE u.belief_id = ? AND c.status = 'sent'",
        (*extra, belief_id))
    return {"held": held["n"], "wanted": held["wanted"], "sent": sent["n"], "answered": sent["answered"], "unwanted": sent["unwanted"]}


def words(f: dict) -> str:
    if not (f["held"] or f["sent"]):
        return ""
    parts = []
    if f["held"]:
        parts.append(f"höll tillbaka {f['held']}, varav {f['wanted']} hämtades eller efterfrågades sedan")
    if f["sent"]:
        parts.append(f"skickade {f['sent']}, varav {f['answered']} besvarades och {f['unwanted']} inte var önskade")
    return "facit: " + "; ".join(parts)


async def for_review(db: Database, role: str) -> str:
    """The role's beliefs whose record has pointed against them since they were last looked at."""
    rows = await db.fetchall(
        "SELECT DISTINCT i.id, i.kind, i.title, i.updated_at, json_extract(i.source, '$.type') AS source FROM items i "
        "JOIN belief_uses u ON u.belief_id = i.id WHERE i.form = 'belief' AND i.status = 'current' AND i.owner = ?", (role,))
    out = []
    for r in rows:
        f = await of(db, r["id"], since=r["updated_at"])
        if not (f["wanted"] or f["unwanted"]):
            continue
        how = ("their own words: don't change it; if its record keeps pointing this way, ask them (find_out)"
               if r["source"] == "owner" else "keep it with a reason (add an observation), change it, or let it go")
        out.append(f"- #U{r['id']} {r['title'][:100]}: {words(f)} ({how})")
    if not out:
        return ""
    return ("Prövning: uppfattningar vars facit pekar emot dem sedan du sist såg över dem (the decisions that leaned on them "
            "held back what the owner then wanted, or sent what they didn't want):\n" + "\n".join(out))
