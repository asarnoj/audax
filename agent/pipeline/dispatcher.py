"""Dispatcher: decides whether, when and how outbox candidates reach användaren (docs/ATTENTION.md §4.4).

Hard rules in code: quiet hours, silence and focus modes, no duplicates. Everything else is judgment against policy.md.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import Settings
from agent.db import Database
from agent.memory import MemoryRepo
from agent.prompts import prompt as prompt_template
from agent.pipeline.outbox import FallbackDispatcher, Outbox, SendPoll
from agent.schedule import Schedule

log = logging.getLogger(__name__)

MODULE = "dispatcher@1"
TIERS = ["interrupt", "next_gap", "bundle", "shelf", "drop"]
# Re-ask about an unchanged outbox at most this often (mechanics, not policy: saves quota).
RECONSIDER_AFTER = timedelta(hours=2)

SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "integer"},
                    "tier": {"type": "string", "enum": TIERS},
                    "send_now": {"type": "boolean"},
                    "reason": {"type": "string", "description": "Swedish, shown to the owner if they ask why"},
                },
                "required": ["candidate_id", "tier", "send_now", "reason"],
            },
        },
    },
    "required": ["decisions"],
}

WEEKDAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]
SILENCE_KEY = "dispatch.silence_until"
FOCUS_KEY = "dispatch.focus_until"


class Dispatcher:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        gate: ClaudeGate,
        memory: MemoryRepo,
        schedule: Schedule,
        outbox: Outbox,
        send_poll: SendPoll,
        manager=None,
        send_proposal=None,
        inbox=None,
    ):
        self.settings = settings
        self.db = db
        self.gate = gate
        self.memory = memory
        self.schedule = schedule
        self.outbox = outbox
        self.send_poll = send_poll
        self.manager = manager  # Manager questions (proposals, check-ins) share the question slot
        self.send_proposal = send_proposal
        self.inbox = inbox
        self.fallback = FallbackDispatcher(outbox)
        self._last_state: tuple | None = None
        self._last_run: datetime | None = None

    # --- user controls ----------------------------------------------------

    async def _until(self, key: str) -> datetime | None:
        v = await self.db.kv_get(key)
        if v and datetime.fromisoformat(v) > self.schedule.now():
            return datetime.fromisoformat(v)
        return None

    async def silence(self, key: str, until: datetime | None) -> None:
        await self.db.kv_set(key, until.isoformat() if until else None)

    async def silenced_until(self) -> datetime | None:
        return await self._until(SILENCE_KEY)

    async def focus_until(self) -> datetime | None:
        return await self._until(FOCUS_KEY)

    # --- main loop ----------------------------------------------------------

    async def run(self, force: bool = False) -> None:
        await self.outbox.expire()
        now = self.schedule.now()
        avail = await self.schedule.availability(now)
        if avail.quiet or await self.silenced_until():
            return
        focus = await self.focus_until() is not None
        busy = avail.current is not None

        pending = await self.outbox.pending()
        if pending:
            # Only spend a Claude run when something changed, or the outbox has waited a while.
            state = (tuple(r["id"] for r in pending), busy)
            stale = self._last_run is None or now - self._last_run >= RECONSIDER_AFTER
            if force or state != self._last_state or stale:
                self._last_state, self._last_run = state, now
                await self._decide(pending, avail, focus)

        if not busy and not focus:
            await self._maybe_ask()

    async def _decide(self, pending: list, avail, focus: bool) -> None:
        context = await self._context(pending, avail)
        try:
            result = await self.gate.run(
                ClaudeTask(
                    kind="dispatcher",
                    prompt=prompt_template("dispatcher").format(**context),
                    priority=Priority.URGENT,
                    model=self.settings.models.dispatcher,
                    output_schema=SCHEMA,
                    max_turns=6,
                )
            )
        except QuotaPaused:
            await self.fallback.run(avail.current is not None, avail.status_line())
            return
        if result.is_error or not isinstance(result.structured, dict):
            log.warning("dispatcher failed (%s), using fallback", result.error)
            await self.fallback.run(avail.current is not None, avail.status_line())
            return

        by_id = {r["id"]: r for r in pending}
        trace = {"availability": context["availability"], "gaps": context["gaps"], "focus": focus}
        to_send = []
        for d in result.structured.get("decisions", []):
            row = by_id.get(d.get("candidate_id"))
            if row is None or d.get("tier") not in TIERS:
                continue
            tier = d["tier"]
            if focus and tier != "interrupt":
                d["send_now"] = False  # focus mode: only interruptions get through
            if tier == "shelf":
                await self.outbox.set_tier(row["id"], tier, "shelved")
            elif tier == "drop":
                await self.outbox.set_tier(row["id"], tier, "dropped")
            else:
                await self.outbox.set_tier(row["id"], tier)
                if d.get("send_now"):
                    to_send.append(row)
            await self.outbox.record(row["id"], MODULE, tier + (" (nu)" if d.get("send_now") and tier not in ("shelf", "drop") else ""), d.get("reason", ""), trace)

        if to_send:
            await self.outbox.deliver(to_send)  # the chat writes the message

    async def _context(self, pending: list, avail) -> dict:
        now = self.schedule.now()
        gaps = await self.schedule.free_windows(now)
        candidates = []
        for r in pending:
            age = now - datetime.fromisoformat(r["created_at"] + "+00:00").astimezone(now.tzinfo)
            candidates.append(
                f"[id {r['id']}] {r['kind']} · {r['title']}\n"
                f"features: {r['features']}\n"
                f"waiting: {int(age.total_seconds() // 3600)} h · earlier decision: {r['tier'] or '-'} · expires: {r['expires_at'] or '-'}\n"
                f"brief: {r['body']}"
            )
        return {
            "now": f"{WEEKDAYS[now.weekday()]} {now:%Y-%m-%d %H:%M}",
            "availability": avail.status_line(),
            "gaps": ", ".join(f"{a:%H:%M}–{b:%H:%M}" for a, b in gaps) or "none left today",
            "activity": await self._activity(),
            "interrupt": self.memory.read("interrupt.md") or "(empty)",
            "policy": self.memory.read("policy.md", "state.md"),
            "candidates": "\n\n".join(candidates),
        }

    async def _activity(self) -> str:
        sent = await self.db.fetchall(
            "SELECT m.id, m.created_at, "
            "(SELECT group_concat(f.signal || ':' || f.value, ', ') FROM feedback f WHERE f.message_id = m.id) AS reactions "
            "FROM messages m WHERE m.direction = 'out' AND m.created_at >= datetime('now', '-1 day') ORDER BY m.id"
        )
        last_in = await self.db.fetchone("SELECT created_at FROM messages WHERE direction = 'in' ORDER BY id DESC LIMIT 1")
        fb = await self.db.fetchall(
            "SELECT signal, value, count(*) AS n FROM feedback WHERE created_at >= datetime('now', '-7 days') "
            "GROUP BY signal, value ORDER BY n DESC LIMIT 10"
        )
        shelf = await self.db.fetchone("SELECT count(*) AS n FROM candidates WHERE status = 'shelved'")
        lines = [
            f"Messages from Audax in the last 24 h: {len(sent)}"
            + (" (" + "; ".join(f"{self._local(r['created_at'])} {r['reactions'] or 'no reaction'}" for r in sent[-8:]) + ")" if sent else ""),
            f"Owner last wrote: {self._local(last_in['created_at']) if last_in else 'never'}",
            "Feedback in the last week: " + (", ".join(f"{r['signal']}:{r['value']}×{r['n']}" for r in fb) or "none"),
            f"On the shelf: {shelf['n']}",
        ]
        return "\n".join(lines)

    async def _maybe_ask(self) -> None:
        """At most one open question at a time, across schedule polls, proposals and check-ins (ATTENTION.md §6)."""
        if await self.db.fetchone("SELECT 1 FROM polls WHERE answered_at IS NULL AND created_at >= datetime('now', '-2 days')"):
            return
        question = await self.schedule.next_question()
        if question is not None:
            poll_id, message_row_id = await self.send_poll(question["question"], question["options"])
            await self.schedule.record_poll(poll_id, message_row_id, question)
            log.info("asked about %d schedule series", len(question["series_ids"]))
            return
        if self.manager is not None and (q := await self.manager.next_question()) is not None:
            await self._ask_manager(q)
            return
        await self._ask_request()

    async def _ask_request(self) -> None:
        """Director's/Manager's free-text questions: the chat session asks them in its own words."""
        if self.inbox is None or self.outbox.speak is None:
            return
        req = await self.inbox.open_request()
        if req is None:
            return
        try:
            ids = await self.outbox.speak(
                f"{req['owner'].capitalize()} wants to know something. Ask the owner naturally and briefly, as part of your "
                f"conversation, not as a form. When they answer later, use route_note with request_id {req['id']}.\n"
                f"What: {req['what']}\nWhy (for you; no need to say it): {req['why']}"
            )
        except Exception as e:
            log.warning("couldn't ask request %s: %s", req["id"], e)
            return
        await self.inbox.mark_asked(req["id"], ids[0] if ids else None)
        log.info("asked request %s from %s", req["id"], req["owner"])

    async def _ask_manager(self, q: dict) -> None:
        if q["type"] == "proposal":
            message_row_id = await self.send_proposal(q["text"], q["id"])
        else:
            poll_id, message_row_id = await self.send_poll(q["question"], q["options"], multiple=False)
            await self.db.execute(
                "INSERT INTO polls (poll_id, kind, payload, message_id) VALUES (?, 'checkin', ?, ?)",
                (poll_id, json.dumps({"options": q["options"]}, ensure_ascii=False), message_row_id),
            )
        await self.manager.mark_asked(q, message_row_id)
        log.info("asked manager question: %s", q["type"])

    def _local(self, sqlite_utc: str) -> str:
        d = datetime.fromisoformat(sqlite_utc + "+00:00").astimezone(self.schedule.tz)
        return f"{WEEKDAYS[d.weekday()]} {d:%H:%M}"
