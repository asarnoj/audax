"""A judgement is as old as what it was judged against.

Triage judges a finding once, against the goals, now.md and the profile as they were that day. What was saved or
planned carried that judgement on for weeks: the OpenAI "rogue" retelling was kept on 1 Nov, the owner said "already
read" on 4 and 5 Nov, it came back from the shelf on 9 Nov to the desk (which judges when, not what) and went out on
10 Nov, the third time. So before a finding from the senses reaches the owner, the code compares when it was judged
with when what triage reads last changed (the owner's own words, Director's now.md, the profile, the office's
practice). If it was judged before, triage looks at it once more, now: keep it, or let it go with a reason. The
content stays Director's judgement; the desk gets no new task.

What the owner has said about the thing is looked up, not carried: the full-text index (agent/search.py) gives each
finding the owner's own messages that match it, so the prompt grows with the findings judged, not with the history.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from agent import facit
from agent.claude_gate import ClaudeTask, Priority, QuotaPaused

log = logging.getLogger(__name__)

MODULE = "rejudge@1"
READS = ("direction/now.md", "profile.md", "lardomar.md")   # what triage judges against (agent/pursuits.py, judged_against)
SAID = 3   # the owner's own messages shown per finding

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "integer"},
                    "keep": {"type": "boolean"},
                    "reason": {"type": "string", "description": "Swedish, one sentence; shown to the owner if they ask why"},
                    "rests_on": {"type": "array", "items": {"type": "string"}, "description": "The beliefs (#U) that decided it, if any"},
                },
                "required": ["candidate_id", "keep", "reason"],
            },
        }
    },
    "required": ["items"],
}



def _at(text: str) -> datetime:
    d = datetime.fromisoformat(text.replace(" ", "T"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def judged_at(row) -> datetime:
    return _at(json.loads(row["features"] or "{}").get("judged_again") or row["created_at"])


async def frames_changed(db, memory) -> datetime | None:
    """When what triage judges against last changed: the owner's own words, and the texts it reads."""
    times = []
    r = await db.fetchone("SELECT max(updated_at) AS t FROM items WHERE form = 'belief' AND json_extract(source, '$.type') = 'owner'")
    if r and r["t"]:
        times.append(_at(r["t"]))
    for name in READS:
        path = memory.path / name
        if path.exists():
            times.append(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
    return max(times) if times else None


async def stale(db, memory, rows) -> list:
    """The findings from the senses judged before what triage reads last changed."""
    changed = await frames_changed(db, memory)
    if changed is None:
        return []
    return [r for r in rows if r["event_id"] is not None and judged_at(r) < changed]


async def _said(db, row, tz: str) -> str:
    from agent.search import search

    hits = [h for h in await search(db, row["title"], limit=12, kinds=("message",)) if h.who == "owner"][:SAID]
    return "\n".join(f"  - {h.line(tz)}" for h in hits) or "  - (nothing found)"


async def run(db, gate, settings, memory, rows) -> dict[int, bool]:
    """Judges the rows again; returns id -> keep. Each is marked judged now, whatever came of it, so it isn't asked twice."""
    from zoneinfo import ZoneInfo

    from agent.prompts import owner, prompt as prompt_template
    from agent.pursuits import judged_against

    tz = settings.timezone
    findings = []
    for r in rows:
        findings.append(f"[candidate_id {r['id']}] {r['kind']} · {r['title']} · judged {judged_at(r):%Y-%m-%d}\n"
                        f"brief: {r['body'][:600]}\n{owner()}'s own words about something like it:\n{await _said(db, r, tz)}")
    prompt = prompt_template("rejudge").format(now=f"{datetime.now(ZoneInfo(tz)):%Y-%m-%d %H:%M}",
                           memory=await judged_against(db, memory, tz), findings="\n\n".join(findings))
    out: dict[int, dict] = {}
    try:
        result = await gate.run(ClaudeTask(kind="triage", prompt=prompt, priority=Priority.URGENT, model=settings.models.triage,
                                           output_schema=SCHEMA, max_turns=3))
        if not result.is_error and isinstance(result.structured, dict):
            out = {i.get("candidate_id"): i for i in result.structured.get("items", [])}
        else:
            log.warning("rejudge failed: %s", result.error)
    except QuotaPaused:
        log.info("rejudge: quota paused; they go as judged before")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    kept: dict[int, bool] = {}
    for r in rows:
        item = out.get(r["id"])
        keep = True if item is None else bool(item.get("keep", True))   # not judged again (quota, error): as judged before
        features = json.loads(r["features"] or "{}")
        features["judged_again"] = now
        await db.execute("UPDATE candidates SET features = ? WHERE id = ?", (json.dumps(features, ensure_ascii=False), r["id"]))
        if item is not None:
            await facit.record(db, item.get("rests_on"), r["id"], MODULE, "keep" if keep else "drop")
            await db.insert("decisions", candidate_id=r["id"], module=MODULE, tier="behållen vid ny prövning" if keep else "släppt vid ny prövning",
                            reason=item.get("reason", ""))
        kept[r["id"]] = keep
    log.info("rejudge: %d judged again, %d let go", len(rows), sum(1 for k in kept.values() if not k))
    return kept
