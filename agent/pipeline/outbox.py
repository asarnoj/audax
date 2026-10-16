"""Outbox: candidates, delivery, the shelf, and the rule-based fallback dispatcher."""

from __future__ import annotations

from agent import trail

import json
import logging
from collections.abc import Awaitable, Callable

from agent.db import Database
from agent.schedule import Schedule, utc_iso

log = logging.getLogger(__name__)

SendText = Callable[..., Awaitable[list[int]]]  # (text) -> messages.id rows
Speak = Callable[[str], Awaitable[list[int]]]  # the chat session writes and sends (docs/ARCHITECTURE.md)
SendPoll = Callable[[str, list[str]], Awaitable[tuple[str, int | None]]]  # -> (poll_id, messages.id)
# Mechanical sanity bound, not UX policy (Manager owns pacing): more proactive messages than this in an hour
# means something is looping, so delivery holds (candidates stay pending) and the health check says so.
BREAKER_PER_HOUR = 6
BREAKER_KEY = "outbox.breaker"
SKIP = "SKIP"  # the chat's answer to a delivery it has already made in the conversation


class AlreadySaid(Exception):
    """Raised by the chat session when a delivery would only repeat what it already said."""


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
    return await db.insert("candidates", event_id=event_id, kind=kind, title=title, body=body, url=url,
                           features=json.dumps(features, ensure_ascii=False), expires_at=expires_at)


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
            "WHERE status IN ('pending', 'scheduled', 'shelved') AND expires_at IS NOT NULL AND expires_at < ?",
            (utc_iso(self.schedule.now()),),
        )

    async def pending(self) -> list:
        return await self.db.fetchall("SELECT * FROM candidates WHERE status = 'pending' ORDER BY created_at")

    async def deliver(self, rows: list) -> None:
        """Sends one message carrying the given candidates and links them for replies, reactions and "why did I get this?".

        At most once per candidate: each one is claimed (pending -> sending) before anything is written, so a second
        concurrent delivery of the same candidate finds nothing to claim. If nothing at all could be sent, the claim
        is released and the candidate waits for the next round."""
        sent = await self.db.fetchone(
            "SELECT count(*) AS n FROM messages WHERE direction = 'out' AND origin = 'proactive' AND created_at >= datetime('now', '-1 hour')"
        )
        if sent["n"] >= BREAKER_PER_HOUR:
            log.warning("delivery held: %d proactive messages in the last hour (breaker)", sent["n"])
            await self.db.kv_set(BREAKER_KEY, utc_iso(self.schedule.now()))
            return
        claimed = []
        for r in rows:
            if await self.db.execute_count(
                "UPDATE candidates SET status = 'sending', updated_at = datetime('now') WHERE id = ? AND status IN ('pending', 'scheduled')", (r["id"],)
            ):
                claimed.append(r)
        if len(claimed) < len(rows):
            log.warning("delivery: %d of %d candidate(s) were no longer pending, skipped", len(rows) - len(claimed), len(rows))
        rows = claimed
        if not rows:
            return
        message_ids: list[int] = []
        try:
            message_ids = await self._send(rows)
        except AlreadySaid:
            for r in rows:
                await self.db.execute("UPDATE candidates SET status = 'dropped', tier = 'drop', updated_at = datetime('now') WHERE id = ?", (r["id"],))
                await self.record(r["id"], "chat", "drop", "Redan sagt i samtalet, så inget nytt meddelande skickades.", {})
            log.info("delivery: the chat had already said this; %d candidate(s) dropped", len(rows))
            return
        except Exception:
            log.exception("delivery failed; the candidates wait for the next round")
        if not message_ids:
            for r in rows:
                await self.db.execute("UPDATE candidates SET status = 'pending' WHERE id = ? AND status = 'sending'", (r["id"],))
            return
        from agent import refs

        await refs.link(self.db, message_ids, {f"#F{r['id']}" for r in rows})
        for r in rows:
            await self.db.execute("UPDATE candidates SET status = 'sent', updated_at = datetime('now') WHERE id = ?", (r["id"],))
            for mid in message_ids:
                await self.db.execute(
                    "INSERT OR IGNORE INTO message_candidates (message_id, candidate_id) VALUES (?, ?)", (mid, r["id"])
                )
        log.info("delivered %d candidate(s)", len(rows))

    async def _send(self, rows: list) -> list[int]:
        """The chat writes the message (one voice); if it can't, the plain draft goes out instead."""
        if self.speak is not None:
            items = "\n".join(f"- {r['title']}: {r['body'].strip()} {r['url'] or ''}".strip() for r in rows)
            try:
                ids = await self.speak(
                    "Deliver this to the owner now: one short message in your usual style, one line per item, links kept. "
                    "If you were just talking about something related, connect to it briefly. If you have already told "
                    f"the owner all of this in this conversation, reply with exactly {SKIP} and nothing is sent.\n"
                    f"Items:\n{items}"
                )
                if ids:
                    return ids
            except AlreadySaid:
                raise
            except Exception as e:
                log.warning("chat session couldn't deliver (%s); sending the draft as-is", e)
        draft = render(rows[0]) if len(rows) == 1 else "\n\n".join(render(r) for r in rows)
        return await self.send_text(draft, candidate_id=rows[0]["id"] if len(rows) == 1 else None)

    async def recover(self) -> None:
        """At startup: a candidate left 'sending' by a crash is sent if a message is linked to it, else pending again."""
        await self.db.execute(
            "UPDATE candidates SET status = 'sent' WHERE status = 'sending' AND id IN (SELECT candidate_id FROM message_candidates)"
        )
        n = await self.db.execute_count("UPDATE candidates SET status = 'pending' WHERE status = 'sending'")
        await self.db.execute("UPDATE bookings SET status = 'planned' WHERE status = 'sending' AND message_id IS NULL")
        if n:
            log.info("outbox: %d interrupted delivery(ies) back to pending", n)

    async def set_tier(self, candidate_id: int, tier: str, status: str | None = None) -> None:
        await self.db.execute(
            "UPDATE candidates SET tier = ?, status = COALESCE(?, status), updated_at = datetime('now') WHERE id = ?",
            (tier, status, candidate_id),
        )

    async def record(self, candidate_id: int, module: str, tier: str, reason: str, context: dict) -> None:
        await self.db.insert("decisions", candidate_id=candidate_id, module=module, tier=tier, reason=reason,
                             context=json.dumps(context, ensure_ascii=False))

    async def peek_shelf(self, limit: int = 12, about: str | None = None) -> list:
        """What is saved for later, newest first, without changing anything (optionally only what matches `about`)."""
        rows = await self.db.fetchall("SELECT * FROM candidates WHERE status = 'shelved' ORDER BY updated_at DESC LIMIT 60")
        if about:
            words = [w for w in about.casefold().split() if len(w) > 2]
            rows = [r for r in rows if any(w in f"{r['title']} {r['body']} {r['kind']}".casefold() for w in words)] or rows
        return rows[:limit]

    async def mark_pulled(self, ids: list[int]) -> int:
        """The owner has been told about these: off the shelf, and a positive signal."""
        n = 0
        for cid in ids:
            if await self.db.execute_count(
                "UPDATE candidates SET status = 'pulled', updated_at = datetime('now') WHERE id = ? AND status = 'shelved'", (cid,)
            ):
                await self.db.insert("feedback", candidate_id=cid, signal="shelf_pull", value="pulled")
                n += 1
        if n:
            await trail.emit("pull", "assistant", n=n)
        return n

    async def pull_shelf(self, limit: int = 8) -> list:
        """What the shelf returns when the owner asks what's new: newest shelved items first. Pulled items count as a positive signal."""
        rows = await self.db.fetchall(
            "SELECT * FROM candidates WHERE status = 'shelved' ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        for r in rows:
            await self.db.execute("UPDATE candidates SET status = 'pulled', updated_at = datetime('now') WHERE id = ?", (r["id"],))
            await self.db.insert("feedback", candidate_id=r["id"], signal="shelf_pull", value="pulled")
        if rows:
            await trail.emit("pull", "assistant", n=len(rows))
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
        for r in rows:
            await self.outbox.set_tier(r["id"], "now")
            await self.outbox.record(r["id"], self.MODULE, "nu", "Reserv: Claude var otillgänglig, så bara det brådskande skickades.", {"availability": context})
        await self.outbox.deliver(rows)  # the rest stays undecided; the desk tries again when Claude is back
