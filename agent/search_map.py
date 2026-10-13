"""The search map (docs/SENSES.md, "Täckning"): seeing what the search misses, not just what it finds.

Measuring each query only shows how good the queries are. It can't tell "there is nothing out there" from
"we are looking in the wrong places". Three things make that visible:

- **The map** (`memory/research/maps/<mission>.md`, written by Researcher): the segments of the space a mission
  searches, where each segment publishes and when, which of Audax's methods cover it, and what is tried next.
- **Leads**: places and organisations a search came across that Audax doesn't watch yet. They accumulate here,
  and Researcher turns the good ones into watched sources, so the net grows over time.
- **Coverage audits**: Researcher lists, independently of what Audax already watches, who typically offers what a
  mission looks for. Python checks each name against everything Audax watches or has seen, and the share
  covered (recall against the benchmark) shows how much is likely missed, and where.

Python gathers and computes; Researcher judges and writes the map (agent/research_plan.py).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from agent.db import Database


def map_path(memory_path: Path, mission: str) -> Path:
    return memory_path / "research" / "maps" / f"{mission}.md"


async def deliveries(db: Database, mission: str, days: int = 14) -> str:
    """What a mission delivered and what became of it: the feedback loop between Researcher and Director."""
    rows = await db.fetchall(
        "SELECT c.id, c.title, c.status, json_extract(c.features, '$.reason') AS why, "
        "(SELECT group_concat(f.value, ' ') FROM feedback f WHERE f.candidate_id = c.id AND f.signal IN ('reaction', 'reply', 'text', 'shelf_pull')) AS owner "
        "FROM events e JOIN candidates c ON c.event_id = e.id WHERE e.source = ? AND e.created_at >= datetime('now', ?) ORDER BY c.id DESC",
        (f"research:{mission}", f"-{days} days"),
    )
    if not rows:
        return f"Delivered, last {days} days: nothing."
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    kept = [r for r in rows if r["status"] in ("sent", "scheduled", "shelved", "pulled", "pending")][:6]
    dropped = [r for r in rows if r["status"] == "dropped"][:4]
    lines = [f"Delivered, last {days} days: {len(rows)} findings ({', '.join(f'{k} {v}' for k, v in counts.items())})."]
    lines += [f"- kept #F{r['id']} {r['title'][:80]} ({r['status']})" + (f" · the owner: {r['owner']}" if r["owner"] else "") for r in kept]
    lines += [f"- dropped {r['title'][:70]}: {(r['why'] or '')[:100]}" for r in dropped]
    return "\n".join(lines)


def lead_key(name: str, url: str | None) -> str:
    if url:
        host = re.sub(r"^https?://(www\.)?", "", url.strip().lower()).split("/")[0]
        if host:
            return host
    return re.sub(r"\W+", " ", name.lower()).strip()


async def record_leads(db: Database, mission: str, leads: list[dict]) -> int:
    """Upserts leads from a search run; returns how many were new."""
    new = 0
    for lead in leads:
        name = (lead.get("name") or "").strip()
        if not name:
            continue
        key = lead_key(name, lead.get("url"))
        cur = await db.execute_count(
            "UPDATE leads SET times = times + 1, last_seen = datetime('now'), why = COALESCE(?, why) WHERE mission = ? AND key = ?",
            (lead.get("why"), mission, key),
        )
        if not cur:
            await db.execute(
                "INSERT INTO leads (mission, key, name, url, kind, why) VALUES (?, ?, ?, ?, ?, ?)",
                (mission, key, name, lead.get("url"), lead.get("kind"), lead.get("why")),
            )
            new += 1
    return new


async def decide_leads(db: Database, decisions: list[dict]) -> list[str]:
    out = []
    for d in decisions:
        status = "added" if d.get("decision") == "add" else "rejected"
        n = await db.execute_count("UPDATE leads SET status = ?, reason = ? WHERE id = ? AND status = 'new'", (status, d.get("reason"), d["id"]))
        if n:
            out.append(f"ledtråd {d['id']} {'lades till' if status == 'added' else 'avfärdades'}")
    return out


def _watched_corpus(sources_file: Path, watchlist_file: Path, research_file: Path) -> str:
    parts = []
    for f in (sources_file, watchlist_file, research_file):
        try:
            parts.append(yaml.safe_dump(yaml.safe_load(f.read_text()) or {}, allow_unicode=True))
        except Exception:
            pass
    return "\n".join(parts).lower()


async def _seen_corpus(db: Database) -> str:
    rows = await db.fetchall("SELECT payload FROM events WHERE created_at >= datetime('now', '-120 days')")
    return "\n".join(r["payload"] for r in rows).lower()


def _fold(text: str) -> str:
    return text.lower().translate(str.maketrans("åäöéü", "aaoeu"))


def _covered(name: str, watched: str, seen: str) -> str | None:
    """'watched' if Audax watches it, 'seen' if it has turned up in what Audax read, else None.
    Matches the full name, the name without a parenthesis, and its first word when that is distinctive
    ("Mojang Studios" -> "mojang"), with å/ä/ö folded so URLs match too."""
    base = _fold(re.sub(r"\s*\(.*?\)", "", name)).strip()
    first = base.split()[0] if base.split() else ""
    variants = {_fold(name).strip(), base} | ({first} if len(first) >= 5 else set())
    variants = {v for v in variants if len(v) >= 3}
    watched, seen = _fold(watched), _fold(seen)
    if any(v in watched for v in variants):
        return "watched"
    if any(v in seen for v in variants):
        return "seen"
    return None


async def audit(db: Database, mission: str, benchmark: list[str], note: str, files: tuple[Path, Path, Path]) -> str:
    """Checks a benchmark (from Director) against what Audax watches or has seen; stores and returns a summary."""
    watched, seen = _watched_corpus(*files), await _seen_corpus(db)
    covered, missing = [], []
    for name in dict.fromkeys(n.strip() for n in benchmark if n.strip()):
        state = _covered(name, watched, seen)
        (covered if state else missing).append(f"{name} ({state})" if state else name)
    total = len(covered) + len(missing)
    recall = len(covered) / total if total else None
    await db.execute(
        "INSERT INTO coverage (mission, benchmark, covered, missing, recall, note) VALUES (?, ?, ?, ?, ?, ?)",
        (mission, json.dumps(benchmark, ensure_ascii=False), json.dumps(covered, ensure_ascii=False),
         json.dumps(missing, ensure_ascii=False), recall, note),
    )
    return f"täckning {mission}: {len(covered)}/{total}" + (f" ({recall:.0%})" if recall is not None else "")


async def mission_stats(db: Database, mission: str) -> dict:
    source = f"research:{mission}"
    runs = await db.fetchone(
        "SELECT count(*) AS n, sum(COALESCE(findings, 0)) AS f, max(created_at) AS last FROM research_runs "
        "WHERE research_id = ? AND created_at >= datetime('now', '-30 days')", (mission,),
    )
    kept = await db.fetchone(
        "SELECT count(*) AS n, max(c.created_at) AS last FROM candidates c JOIN events e ON e.id = c.event_id "
        "WHERE e.source = ? AND c.status IN ('sent', 'shelved', 'pulled', 'pending', 'sending')", (source,),
    )
    queries = await db.fetchone(
        "SELECT sum(status = 'active') AS active, sum(status = 'retired') AS retired FROM scout_queries WHERE mission = ?", (mission,),
    )
    return {
        "runs_30d": runs["n"] or 0, "findings_30d": runs["f"] or 0, "last_run": runs["last"],
        "kept_total": kept["n"] or 0, "last_kept": kept["last"],
        "queries_active": queries["active"] or 0, "queries_retired": queries["retired"] or 0,
    }


async def overview(db: Database, memory_path: Path, mission: str, with_map: bool = True) -> str:
    """One mission's strategy at a glance: the map, how it is going, leads, and the latest coverage audit."""
    st = await mission_stats(db, mission)
    lines = [
        f"## {mission}",
        f"Last 30 days: {st['runs_30d']} runs, {st['findings_30d']} findings. Kept in total: {st['kept_total']}"
        + (f", latest {st['last_kept'][:10]}" if st["last_kept"] else ", none yet")
        + f". Queries: {st['queries_active']} active, {st['queries_retired']} retired.",
    ]
    cov = await db.fetchone("SELECT * FROM coverage WHERE mission = ? ORDER BY id DESC LIMIT 1", (mission,))
    if cov:
        missing = json.loads(cov["missing"])
        lines.append(
            f"Coverage audit {cov['created_at'][:10]}: {len(json.loads(cov['covered']))} of {len(json.loads(cov['benchmark']))} in the benchmark"
            + (f" ({cov['recall']:.0%})" if cov["recall"] is not None else "")
            + (f". Missing: {', '.join(missing[:15])}" if missing else ". Nothing missing.")
        )
    else:
        lines.append("Coverage audit: none yet.")
    leads = await db.fetchall(
        "SELECT id, name, url, kind, why, times FROM leads WHERE mission = ? AND status = 'new' ORDER BY times DESC, id DESC LIMIT 12", (mission,),
    )
    if leads:
        lines.append("New leads (id · name · kind · times seen · why):")
        lines += [f"- [{r['id']}] {r['name']} {r['url'] or ''} · {r['kind'] or '?'} · {r['times']}× · {r['why'] or ''}".rstrip() for r in leads]
    if with_map:
        p = map_path(memory_path, mission)
        lines.append("Map:\n" + (p.read_text().strip() if p.exists() else "(no map yet)"))
    return "\n".join(lines)


async def gaps(memory_path: Path, mission: str) -> str:
    """The part of the map a search run should aim its exploring at: segments marked thin or uncovered."""
    p = map_path(memory_path, mission)
    if not p.exists():
        return ""
    text = p.read_text()
    m = re.search(r"(?ims)^#+\s*(luckor|gaps|nästa|next)[^\n]*\n(.*?)(?=^#|\Z)", text)
    return (m.group(2).strip() if m else "")[:1500]
