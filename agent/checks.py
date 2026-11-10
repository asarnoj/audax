"""Unsure means look (instance/granskning/2026-11-09-omgang-12-notisen.md, stage 2).

A promising finding that lacks a deciding fact (a date, whether registration is needed, whether it is still open) used
to be shelved "until it is confirmed", and nobody was asked to confirm it: the Grok Bot meetup sat on the shelf from
26 Oct with its date one page away. Now triage or the desk names what is unclear, and a short run reads the page and
searches if needed. The finding comes back to the desk with what was confirmed, once: what is still unclear goes out
said as unclear (the `unsure` feature), or is dropped with a reason.
"""

from __future__ import annotations

import json
import logging

from agent import timing
from agent.claude_gate import ClaudeTask, Priority, QuotaPaused

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "confirmed": {"type": "string", "description": "Swedish: what you found, with the facts (dates, place, how to sign up, deadline)"},
        "still_unclear": {"type": ["string", "null"], "description": "Swedish: what you couldn't settle, or null"},
        "verdict": {"type": "string", "enum": ["keep", "drop"],
                    "description": "drop only if what you found makes it worthless to the owner (over, closed, not what it seemed)"},
        "why": {"type": "string", "description": "Swedish, one sentence"},
        "expires": {"type": ["string", "null"], "description": "YYYY-MM-DD after which it is worth nothing (event day, deadline), or null"},
        **timing.SCHEMA,
    },
    "required": ["confirmed", "verdict", "why"],
}

PROMPT = """You check one finding for {owner} before Audax decides whether to tell them. Something deciding is unclear;
find it out. Read the finding's page first; search the web only if the page doesn't settle it. Be quick and factual:
a few reads, not an investigation. Report what you found, with the facts that matter, and what is still unclear.
Say when {owner} must know at the latest from what you found (registration, approval, a waiting list), if that differs
from what is written below or nothing is.

Today: {today}
Finding: {title}
What triage wrote: {body}
Address: {url}
Must know at the latest: {latest}
What is unclear: {unclear}
"""


def needs(row) -> str | None:
    """What a pending finding needs checked, if it hasn't been checked yet."""
    f = json.loads(row["features"] or "{}")
    return None if f.get("checked") else (f.get("check") or None)


async def check(db, gate, settings, web, row, owner: str, today: str) -> bool:
    """Runs the check and writes it into the finding; it goes back to the desk (pending) or is dropped. True when done."""
    from agent.pipeline.outbox import Outbox
    from agent.schedule import utc_iso
    from agent.tools import TurnContext, build_web_tools

    unclear = needs(row)
    servers, names = build_web_tools(web, TurnContext()) if web else ({}, [])
    prompt = PROMPT.format(owner=owner, today=today, title=row["title"], body=row["body"], url=row["url"] or "(none)", unclear=unclear,
                           latest=timing.show(row, settings.timezone))
    try:
        result = await gate.run(ClaudeTask(kind="check", prompt=prompt, priority=Priority.URGENT, model=settings.models.dispatcher,
                                           allowed_tools=["WebSearch", *names], mcp_servers=servers, output_schema=SCHEMA, max_turns=12))
    except QuotaPaused:
        return False
    out = result.structured
    if result.is_error or not isinstance(out, dict):
        log.warning("check of #F%s failed: %s", row["id"], result.error or "no structured answer")
        out = {"confirmed": "", "still_unclear": unclear, "verdict": "keep", "why": "Kontrollen gick inte att göra."}
    features = json.loads(row["features"] or "{}")
    features["checked"] = unclear
    if out.get("confirmed"):
        features["check_result"] = out["confirmed"]
    if out.get("still_unclear"):
        features["unsure"] = out["still_unclear"]   # it goes out said as unclear, or is dropped
    body = row["body"] + (f"\nKontrollerat: {out['confirmed']}" if out.get("confirmed") else "")
    expires = row["expires_at"]
    if out.get("expires"):
        try:
            from datetime import datetime, timezone

            expires = utc_iso(datetime.fromisoformat(out["expires"][:10]).replace(hour=23, minute=59, tzinfo=timezone.utc))
        except ValueError:
            pass
    latest = row["latest_useful"]
    if found := timing.parse(out.get("latest_useful"), settings.timezone):
        latest = found   # what the page said about registration and approval decides it
        if out.get("latest_why"):
            features["latest_why"] = out["latest_why"]
    drop = out.get("verdict") == "drop"
    await db.execute("UPDATE candidates SET body = ?, features = ?, expires_at = ?, latest_useful = ?, status = ?, updated_at = datetime('now') WHERE id = ?",
                     (body, json.dumps(features, ensure_ascii=False), expires, latest, "dropped" if drop else "pending", row["id"]))
    if drop:
        await Outbox(db, None, None).record(row["id"], "check@1", "släppt efter kontroll", out.get("why", ""), {"checked": unclear})
    log.info("checked #F%s (%s): %s", row["id"], unclear, "dropped" if drop else "back to the desk")
    return True
