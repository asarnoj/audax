"""Outbox: candidates, delivery, the shelf, and the rule-based fallback dispatcher."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from agent.db import Database
from agent.schedule import Schedule, utc_iso

log = logging.getLogger(__name__)

SendText = Callable[..., Awaitable[list[int]]]  # (text) -> messages.id rows
Speak = Callable[[str], Awaitable[list[int]]]  # the chat session writes and sends (docs/ARCHITECTURE.md)
SendPoll = Callable[[str, list[str]], Awaitable[tuple[str, int | None]]]  # -> (poll_id, messages.id)


async def add_candidate(
    db: Database,
    kind: str,
    title: str,
    body: str,
    features: dict,
    url: str | None = None,
    expires_at: str | None = None,
    event_id: int | None = None,
) -> int:
    return await db.execute(
        "INSERT INTO candidates (event_id, kind, title, body, url, features, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_id, kind, title, body, url, json.dumps(features, ensure_ascii=False), expires_at),
    )


def render(row) -> str:
    """A candidate as message text; makes sure the link is included."""
    body = row["body"].strip()
    if row["url"] and row["url"] not in body:
        body += f"\n{row['url']}"
    return body


class Outbox:
    def __init__(self, db: Database, schedule: Schedule, send_text: SendText, speak: Speak | None = None):
        self.db = db
        self.schedule = schedule
        self.send_text = send_text
        self.speak = speak  # set when the chat session exists; without it drafts are sent as-is

    async def expire(self) -> None:
        await self.db.execute(
            "UPDATE candidates SET status = 'expired', updated_at = datetime('now') "
            "WHERE status IN ('pending', 'shelved') AND expires_at IS NOT NULL AND expires_at < ?",
            (utc_iso(self.schedule.now()),),
        )

    async def pending(self) -> list:
        return await self.db.fetchall("SELECT * FROM candidates WHERE status = 'pending' ORDER BY created_at")

    async def deliver(self, rows: list, text: str | None = None) -> None:
        """Sends one message carrying the given candidates and links them for replies, reactions and /varför."""
        if not rows:
            return
        draft = text or (render(rows[0]) if len(rows) == 1 else "\n\n".join(render(r) for r in rows))
        message_ids: list[int] = []
        if self.speak is not None:
            items = "\n".join(f"- {r['title']}: {r['body'].strip()} {r['url'] or ''}".strip() for r in rows)
            try:
                message_ids = await self.speak(
                    "Dispatchern har bestämt att det här ska fram nu. Skriv meddelandet till användaren enligt stilen: "
                    "kort, en rad per sak, med länkarna. Om ni nyss pratat om något som hör ihop, knyt an kort.\n"
                    f"Underlag:\n{items}" + (f"\n\nFörslag på formulering:\n{text}" if text else "")
                )
            except Exception as e:
                log.warning("chat session couldn't deliver (%s); sending the draft as-is", e)
        if not message_ids:
            message_ids = await self.send_text(draft, candidate_id=rows[0]["id"] if len(rows) == 1 else None)
        for r in rows:
            await self.db.execute("UPDATE candidates SET status = 'sent', updated_at = datetime('now') WHERE id = ?", (r["id"],))
            for mid in message_ids:
                await self.db.execute(
                    "INSERT OR IGNORE INTO message_candidates (message_id, candidate_id) VALUES (?, ?)", (mid, r["id"])
                )
        log.info("delivered %d candidate(s)", len(rows))

    async def set_tier(self, candidate_id: int, tier: str, status: str | None = None) -> None:
        await self.db.execute(
            "UPDATE candidates SET tier = ?, status = COALESCE(?, status), updated_at = datetime('now') WHERE id = ?",
            (tier, status, candidate_id),
        )

    async def record(self, candidate_id: int, module: str, tier: str, reason: str, context: dict) -> None:
        await self.db.execute(
            "INSERT INTO decisions (candidate_id, module, tier, reason, context) VALUES (?, ?, ?, ?, ?)",
            (candidate_id, module, tier, reason, json.dumps(context, ensure_ascii=False)),
        )

    async def pull_shelf(self, limit: int = 8) -> list:
        """What /nytt shows: newest shelved items first. Pulled items count as a positive signal."""
        rows = await self.db.fetchall(
            "SELECT * FROM candidates WHERE status = 'shelved' ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        for r in rows:
            await self.db.execute("UPDATE candidates SET status = 'pulled', updated_at = datetime('now') WHERE id = ?", (r["id"],))
            await self.db.execute(
                "INSERT INTO feedback (candidate_id, signal, value) VALUES (?, 'shelf_pull', 'pulled')", (r["id"],)
            )
        return rows


class FallbackDispatcher:
    """Rule-based dispatcher used when Claude is unavailable (quota paused, errors).

    Only time-critical items go out, and never during quiet hours.
    """

    MODULE = "fallback@1"

    def __init__(self, outbox: Outbox):
        self.outbox = outbox

    async def run(self, busy: bool, context: str) -> None:
        rows = [r for r in await self.outbox.pending() if json.loads(r["features"]).get("time_sensitivity") == "high"]
        if not rows:
            return
        await self.outbox.deliver(rows)
        for r in rows:
            tier = "interrupt" if busy else "next_gap"
            await self.outbox.set_tier(r["id"], tier)
            await self.outbox.record(r["id"], self.MODULE, tier, "reserv: Claude otillgänglig, bara brådskande skickas", {"availability": context})
