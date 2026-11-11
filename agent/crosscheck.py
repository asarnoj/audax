"""Each role reviews the fast skill that is its own (instance/granskning/2026-11-10-omgang-14-tid-och-granskning.md, B).

Manager spot checks its desk every night. Triage, Director's fast skill, lets about forty findings a day go, and nobody
looked: the test cases (agent/cases.py) can never show one, since the owner never sees what was dropped and so can't
fetch or like it. So Director's daily review gets a sample of what triage did since the last one: some drops at random,
every drop that leaned on a belief which has since changed or been struck, and what it kept that the owner didn't
want. A drop Director finds wrong comes back to the desk while it is still current, counts against the beliefs it
leaned on, and the fix goes where triage reads: now.md and the goals. Researcher sees triage's reasons for what its own
missions brought and was dropped, and questions Director with a post when they look wrong; Manager's sample of the desk
shows the beliefs each decision leaned on and what was held until its moment passed.
"""

from __future__ import annotations

import json
import random

from agent.db import Database

KEY = "director.triage_reviewed"
AT_RANDOM = 5   # drops shown at random besides those that leaned on a changed belief
PER_MISSION = 2

SCHEMA = {
    "type": "object",
    "description": "Your check of triage's sample in the review (only when there is one)",
    "properties": {
        "checked": {"type": "integer", "description": "How many of the sample you looked at"},
        "wrong": {"type": "array", "description": "Drops that were wrong: they come back to the desk while still current",
                  "items": {"type": "object", "properties": {"id": {"type": "string", "description": "#F123"},
                                                            "why": {"type": "string", "description": "Swedish"}},
                            "required": ["id", "why"]}},
        "notes": {"type": "string", "description": "Swedish: what the sample says about triage, and what you changed in now.md or the goals"},
    },
    "required": ["checked", "notes"],
}


def _uses(rows) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["candidate_id"], []).append(f"#U{r['belief_id']}")
    return out


async def triage_sample(db: Database, since: str, rng: random.Random | None = None) -> tuple[str, list[int]]:
    """What Director looks at: (text for the review, the ids shown)."""
    rng = rng or random.Random()
    drops = await db.fetchall(
        "SELECT c.id, c.kind, c.title, c.body, d.reason FROM decisions d JOIN candidates c ON c.id = d.candidate_id "
        "WHERE d.module = 'triage@1' AND d.tier = 'drop' AND d.created_at >= ? ORDER BY d.id", (since,))
    kept = (await db.fetchone("SELECT count(*) AS n FROM candidates c WHERE c.event_id IS NOT NULL AND c.created_at >= ? "
                              "AND c.status != 'dropped'", (since,)))["n"]
    if not drops and not kept:
        return "", []
    changed = {r["candidate_id"] for r in await db.fetchall(
        "SELECT u.candidate_id FROM belief_uses u JOIN items i ON i.id = u.belief_id "
        "WHERE u.module = 'triage@1' AND u.decision = 'drop' AND u.created_at >= ? AND (i.updated_at > u.created_at OR i.status != 'current')",
        (since,))}
    by_id = {r["id"]: r for r in drops}
    pick = [i for i in by_id if i in changed]
    rest = [i for i in by_id if i not in changed]
    pick += rng.sample(rest, min(AT_RANDOM, len(rest)))
    unwanted = await db.fetchall(
        "SELECT DISTINCT c.id, c.kind, c.title, c.body, json_extract(c.features, '$.reason') AS reason FROM candidates c "
        "JOIN message_candidates mc ON mc.candidate_id = c.id JOIN feedback f ON f.message_id = mc.message_id OR f.candidate_id = c.id "
        "WHERE c.event_id IS NOT NULL AND c.status = 'sent' AND f.created_at >= ? "
        "AND f.value IN ('negative:amount', 'negative:timing', 'negative:content', '👎')", (since,))
    shown = pick + [r["id"] for r in unwanted]
    if not shown:
        return "", []
    marks = ",".join("?" * len(shown))
    uses = _uses(await db.fetchall(f"SELECT candidate_id, belief_id FROM belief_uses WHERE module = 'triage@1' AND candidate_id IN ({marks})",
                                   tuple(shown)))

    def line(r, note: str = "") -> str:
        leaned = ", ".join(uses.get(r["id"], [])) or "nothing named"
        return (f"- #F{r['id']} {r['kind']} · {r['title'][:90]}{note}\n  triage: {(r['reason'] or '')[:200]} (leaned on: {leaned})\n"
                f"  {(r['body'] or '')[:200]}")

    lines = [f"Triagens stickprov (your fast skill, since {since[:16]} UTC: {len(drops)} dropped, {kept} kept). Check each "
             "against what you know of the owner now. A wrong drop goes in `triage_review.wrong` and comes back to the desk; "
             "the fix goes where triage reads, in now.md or the goals. A belief it leaned on that keeps misleading it is "
             "yours to change."]
    if pick:
        lines.append("Dropped (those that leaned on a belief that has since changed come first):")
        lines += [line(by_id[i], " · its belief has changed since" if i in changed else "") for i in pick]
    if unwanted:
        lines.append("Kept, and the owner didn't want it:")
        lines += [line(r) for r in unwanted]
    return "\n".join(lines), shown


async def apply_review(db: Database, review: dict | None, shown: list[int], now_iso: str) -> list[str]:
    """Director's check of the sample: wrong drops back to the desk while current; each counts against what it leaned on."""
    if not isinstance(review, dict):
        return []
    await db.kv_set(KEY, now_iso)
    changes = [f"stickprov på triagen: {review.get('checked', 0)} granskade"]
    for w in review.get("wrong") or []:
        digits = "".join(ch for ch in str(w.get("id", "")) if ch.isdigit())
        if not digits or int(digits) not in shown:
            continue
        cid = int(digits)
        row = await db.fetchone("SELECT * FROM candidates WHERE id = ?", (cid,))
        if row is None or row["status"] != "dropped":
            continue
        await db.insert("feedback", candidate_id=cid, signal="triage_review", value="wrongly_dropped", note=w.get("why", ""))
        await db.insert("decisions", candidate_id=cid, module="director", tier="fel att släppa", reason=w.get("why", ""))
        current = await db.fetchone("SELECT 1 FROM candidates WHERE id = ? AND (expires_at IS NULL OR expires_at > ?)", (cid, now_iso))
        if current:
            features = json.loads(row["features"] or "{}")
            features["returned"] = f"Director tog tillbaka det från triagens släppta: {w.get('why', '')}"
            features["judged_again"] = now_iso   # Director's judgement is the newest: it isn't judged again on its way out
            await db.execute("UPDATE candidates SET status = 'pending', tier = NULL, features = ?, updated_at = datetime('now') WHERE id = ?",
                             (json.dumps(features, ensure_ascii=False), cid))
        changes.append(f"#F{cid} fel att släppa" + (", tillbaka till desken" if current else ", men inte längre aktuellt"))
    return changes


async def mission_drops(db: Database, days: int = 7) -> str:
    """For Researcher: triage's reasons for what its own missions brought and was dropped."""
    rows = await db.fetchall(
        "SELECT e.source, c.id, c.title, d.reason FROM decisions d JOIN candidates c ON c.id = d.candidate_id "
        "JOIN events e ON e.id = c.event_id WHERE d.module = 'triage@1' AND d.tier = 'drop' AND e.source LIKE 'research:%' "
        "AND d.created_at >= datetime('now', ?) ORDER BY d.id DESC", (f"-{days} days",))
    if not rows:
        return ""
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["source"].removeprefix("research:"), []).append(r)
    lines = [f"Triagens skäl för det dina uppdrag hittade och som släpptes ({days} dagar). Triage is Director's: if a reason "
             "looks wrong (it misreads the owner, or drops what the mission was for), question it with a post to Director; "
             "if the mission brings the wrong things, change the mission."]
    for mission, rs in by.items():
        lines.append(f"- {mission}: {len(rs)} släppta, t.ex.")
        lines += [f"  · #F{r['id']} {r['title'][:80]}: {(r['reason'] or '')[:160]}" for r in rs[:PER_MISSION]]
    return "\n".join(lines)
