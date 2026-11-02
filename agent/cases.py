"""The search's test cases (instance/granskning/2026-11-02-omgang-10-sokningen.md, stage 2): what turned out to matter,
and whether the search brought it.

A source's yield (agent/due.py) says how much of what it brings is kept. It can't say what the search never saw: a
competition the owner found themselves, a programme they applied to that came from a friend. So the code keeps the
things that turned out to matter, from five places, and works out for each how Audax got it:

- past_round: an earlier round an efterlysning found (agent/wanted.py)
- miss: something the owner found themselves that Audax should have caught
- pursued: something the owner went for (#S pursuit with an address)
- pulled: something the owner fetched from the shelf
- liked: something the owner reacted to with a thumbs up

Each is `caught` (a source brought it and it was kept), `dropped` (a source brought it and it was let go: a matter of
judgement, not of search), `missed` (no source ever saw it), or `before` (it happened before Audax watched anything).
How many of what mattered each kind of method brought is the measure the method experiments are judged by; the
cases themselves are what Researcher reads for where the search is blind.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

from agent.db import Database
from agent.items import Items

log = logging.getLogger(__name__)

KEPT = ("sent", "shelved", "pulled", "scheduled")
NEAR = timedelta(days=60)   # an event this close to the case's date may be the case
POSITIVE = ("👍", "❤", "❤️", "🔥", "🎉", "👏")
SHOWN = 8


def _norm(url: str | None) -> str:
    from agent.research import normalize_url

    return normalize_url(url).lower().removeprefix("https://").removeprefix("http://").removeprefix("www.") if url else ""


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", (text or "").casefold()) if len(w) > 3}


def _same(case_title: str, title: str) -> bool:
    want = _words(case_title)
    return len(want) >= 2 and len(want & _words(title)) >= 0.7 * len(want)


async def _method(db: Database, source: str) -> str:
    """The kind of method a source is: what the measures are kept per."""
    from agent import routines

    if source.startswith("research:"):
        return "search mission"
    if source.startswith("wanted:"):
        return "efterlysning"
    if source.startswith("stream:"):
        r = await routines.find(db, "stream", source.removeprefix("stream:"))
        kind = r["_config"]["stream"].get("type") if r else None
        return {"topic": "news search", "rss": "feed", "page": "page", "ats": "job board", "jobtech": "job board",
                "devpost": "event site", "hn_hiring": "job board"}.get(kind or "", kind or "stream")
    return source.split(":")[0] or "other"


INDEPENDENT = ("past_round", "miss", "pursued")   # cases that exist whether or not the search saw them: the measure
                                                  # (what was pulled or liked was brought by the search by definition)


async def _classify(db: Database, url: str | None, title: str, happened: str | None, start: str, origin: str = "") -> dict:
    """How Audax got it: the event that brought it (if any) and what became of it."""
    when = None
    try:
        when = date.fromisoformat(happened[:10]) if happened else None
    except ValueError:
        pass
    sql, args = "SELECT e.id, e.source, e.payload, e.created_at, c.id AS cid, c.status FROM events e " \
                "LEFT JOIN candidates c ON c.event_id = e.id", ()
    if when is not None:
        sql += " WHERE e.created_at BETWEEN ? AND ?"
        args = (f"{when - NEAR}", f"{when + NEAR} 23:59:59")
    target = _norm(url)
    best = None
    for r in await db.fetchall(sql, args):
        if origin == "past_round" and r["source"].startswith("wanted:"):
            continue   # the investigation that found the earlier round isn't a source that saw it
        try:
            p = json.loads(r["payload"] or "{}")
        except ValueError:
            continue
        if (target and _norm(p.get("url")) == target) or _same(title, p.get("title") or ""):
            if best is None or (r["status"] in KEPT and best["status"] not in KEPT):
                best = dict(r)
    if best is None:
        if when is not None and f"{when}" < start:
            return {"status": "before"}
        return {"status": "missed"}
    status = "caught" if best["status"] in KEPT else "dropped"
    return {"status": status, "source": best["source"], "event_id": best["id"], "candidate_id": best["cid"]}


async def _gather(db: Database) -> list[dict]:
    out = []
    items = Items(db)
    for w in await items.find("wanted"):
        for p in json.loads(w.get("past") or "[]") or []:
            out.append({"origin": "past_round", "title": p.get("title") or w["title"], "url": p.get("url"),
                        "happened": p.get("date"), "ref": f"#J{w['id']}"})
        if w["kind"] == "miss" and w.get("missed_url"):
            out.append({"origin": "miss", "title": w.get("interpretation") or w["title"], "url": w["missed_url"],
                        "happened": w["created_at"][:10], "ref": f"#J{w['id']}"})
    for s in await items.find("aim", kind="pursuit", where="json_extract(data, '$.url') IS NOT NULL"):
        if s["status"] in ("declined", "dropped"):
            continue
        out.append({"origin": "pursued", "title": s["title"], "url": s.get("url"), "happened": s["created_at"][:10], "ref": f"#S{s['id']}"})
    for r in await db.fetchall(
        "SELECT c.id, c.title, c.updated_at, e.payload FROM candidates c JOIN events e ON e.id = c.event_id WHERE c.status = 'pulled'"):
        out.append({"origin": "pulled", "title": r["title"], "url": json.loads(r["payload"] or "{}").get("url"),
                    "happened": r["updated_at"][:10], "ref": f"#F{r['id']}"})
    marks = ",".join("?" * len(POSITIVE))
    for r in await db.fetchall(
        "SELECT DISTINCT c.id, c.title, c.created_at, e.payload FROM feedback f "
        "JOIN message_candidates mc ON mc.message_id = f.message_id JOIN candidates c ON c.id = mc.candidate_id "
        f"JOIN events e ON e.id = c.event_id WHERE f.signal = 'reaction' AND f.value IN ({marks})", POSITIVE):
        out.append({"origin": "liked", "title": r["title"], "url": json.loads(r["payload"] or "{}").get("url"),
                    "happened": r["created_at"][:10], "ref": f"#F{r['id']}"})
    return out


async def collect(db: Database) -> int:
    """Brings the cases up to date. Returns how many are new or changed."""
    first = await db.fetchone("SELECT min(created_at) AS t FROM events")
    start = (first["t"] or "9999")[:10] if first else "9999"
    recheck = (await db.fetchone("SELECT datetime('now', '-1 day') AS t"))["t"]   # an open case is looked at again daily
    changed = 0
    for c in await _gather(db):
        key = _norm(c["url"]) or f"title:{' '.join(sorted(_words(c['title'])))}"
        old = await db.fetchone("SELECT id, status, updated_at FROM search_cases WHERE key = ?", (key,))
        if old is not None and (old["status"] in ("caught", "before") or old["updated_at"] >= recheck):
            continue   # settled, or looked at today
        got = await _classify(db, c["url"], c["title"], c["happened"], start, c["origin"])
        method = await _method(db, got["source"]) if got.get("source") else None
        if old is None:
            await db.execute(
                "INSERT INTO search_cases (key, origin, title, url, happened, ref, status, source, method, event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, c["origin"], c["title"][:200], c["url"], c["happened"], c["ref"], got["status"], got.get("source"),
                 method, got.get("event_id")))
            changed += 1
        elif old["status"] != got["status"]:
            await db.execute("UPDATE search_cases SET status = ?, source = ?, method = ?, event_id = ?, reviewed_at = NULL, "
                             "updated_at = datetime('now') WHERE id = ?",
                             (got["status"], got.get("source"), method, got.get("event_id"), old["id"]))
            changed += 1
        else:
            await db.execute("UPDATE search_cases SET updated_at = datetime('now') WHERE id = ?", (old["id"],))
    return changed


async def unreviewed(db: Database) -> list[dict]:
    """Cases the search failed on that Researcher hasn't seen yet."""
    return [dict(r) for r in await db.fetchall(
        "SELECT * FROM search_cases WHERE status IN ('missed', 'dropped') AND reviewed_at IS NULL ORDER BY id")]


async def mark_reviewed(db: Database) -> None:
    await db.execute("UPDATE search_cases SET reviewed_at = datetime('now') WHERE reviewed_at IS NULL")


async def report(db: Database) -> str:
    """For Researcher's review: how much of what mattered the search brought, per kind of method, and what it didn't."""
    rows = [dict(r) for r in await db.fetchall("SELECT * FROM search_cases ORDER BY id DESC")]
    counted = [r for r in rows if r["status"] != "before"]
    if not counted:
        return ""
    measure = [r for r in counted if r["origin"] in INDEPENDENT]
    n = {s: sum(1 for r in measure if r["status"] == s) for s in ("caught", "dropped", "missed")}
    lines = [f"Testfall (what turned out to matter, and whether the search brought it). Of {len(measure)} that exist whether "
             f"or not the search saw them (earlier rounds, misses, what the owner went for): {n['caught']} caught, "
             f"{n['dropped']} seen but dropped, {n['missed']} never seen"
             + (f" (the search brought {100 * n['caught'] / len(measure):.0f} %)." if measure else ".")
             + f" Besides, {len(counted) - len(measure)} the owner fetched or liked, all brought by the search."]
    by_method: dict[str, int] = {}
    for r in counted:
        if r["status"] == "caught":
            by_method[r["method"] or "?"] = by_method.get(r["method"] or "?", 0) + 1
    if by_method:
        lines.append("Caught by: " + ", ".join(f"{m} {k}" for m, k in sorted(by_method.items(), key=lambda x: -x[1])))
    by_origin: dict[str, list[int]] = {}
    for r in counted:
        got = by_origin.setdefault(r["origin"], [0, 0])
        got[0] += r["status"] == "caught"
        got[1] += 1
    lines.append("By where the case comes from (caught/all): " + ", ".join(f"{o} {a}/{b}" for o, (a, b) in by_origin.items()))
    failed = [r for r in counted if r["status"] != "caught"][:SHOWN]
    if failed:
        lines.append("Not brought (newest first; a missed one is a blind spot of the search, a dropped one a judgement to "
                     "question with Director, who owns triage):")
        for r in failed:
            why = ""
            if r["status"] == "dropped" and r["event_id"]:
                d = await db.fetchone("SELECT d.reason FROM decisions d JOIN candidates c ON c.id = d.candidate_id "
                                      "WHERE c.event_id = ? ORDER BY d.id DESC LIMIT 1", (r["event_id"],))
                why = f" · dropped: {d['reason'][:140]}" if d else " · dropped before triage"
            lines.append(f"  - [{r['status']}] {r['title'][:90]} ({r['url'] or '-'}) · {r['origin']} {r['ref'] or ''}"
                         + (f" · via {r['source']}" if r["source"] else "") + why)
    return "\n".join(lines)
