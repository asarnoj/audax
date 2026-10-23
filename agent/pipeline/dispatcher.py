"""Dispatcher: Manager's fast task (docs/MODEL.md). When should each new finding reach the owner?

It gives every new finding a delivery time (now, or a planned moment), or saves it for later, or lets it go, under
Manager's policy. Planned deliveries go into the schedule (agent/agenda.py); `tick()` runs every minute in Python and
delivers what is due after a last check (quiet hours, a pause, the owner busy in a session), so no Claude run is spent
waiting. It only thinks again when something new arrives or the situation changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from agent import trail
from agent.agenda import FOCUS_KEY, SILENCE_KEY, Agenda
from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import Settings
from agent.db import Database
from agent.memory import MemoryRepo
from agent.prompts import prompt as prompt_template
from agent.pipeline.outbox import AlreadySaid, FallbackDispatcher, Outbox, SendPoll
from agent.schedule import Schedule, utc_iso

log = logging.getLogger(__name__)

MODULE = "dispatcher@2"
DECISIONS = ["now", "at", "shelf", "drop"]
RETRY_UNDECIDED = timedelta(minutes=10)  # mechanics: a finding nobody could decide on (quota, error) is retried
ASK_EVERY = timedelta(minutes=15)  # mechanics: how often an idle moment is checked for an open question

SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "integer"},
                    "decision": {"type": "string", "enum": DECISIONS},
                    "at": {"type": ["string", "null"], "description": "For 'at': local time, YYYY-MM-DD HH:MM"},
                    "reason": {"type": "string", "description": "Swedish, shown to the owner if they ask why"},
                },
                "required": ["candidate_id", "decision", "reason"],
            },
        },
    },
    "required": ["decisions"],
}

WEEKDAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]


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
        self._lock = asyncio.Lock()  # one at a time: deciding and delivering never overlap
        self.gate = gate
        self.memory = memory
        self.schedule = schedule
        self.agenda = Agenda(db, schedule)
        self.outbox = outbox
        self.send_poll = send_poll
        self.manager = manager  # Manager questions (proposals, check-ins) share the question slot
        self.send_proposal = send_proposal
        self.inbox = inbox
        self.fallback = FallbackDispatcher(outbox)
        self._last_decide: datetime | None = None
        self._last_ask: datetime | None = None

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

    # --- deciding: when should each new finding reach the owner? -----------------

    async def run(self, force: bool = False) -> None:
        """Decides on findings nobody has decided on yet (called when something new arrives or the situation changes)."""
        async with self._lock:
            await self._prepare()
            pending = await self.outbox.pending()
            if pending:
                await self._decide(pending)

    async def _prepare(self) -> None:
        from agent.pursuits import Pursuits

        await Pursuits(self.db, self.settings.timezone).tick()  # missed deadlines; due nudges join the outbox
        await self.outbox.expire()

    async def _decide(self, pending: list) -> None:
        self._last_decide = self.schedule.now()
        avail = await self.schedule.availability()
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
        trace = {"availability": context["availability"], "gaps": context["gaps"]}
        now_rows = []
        for d in result.structured.get("decisions", []):
            row = by_id.get(d.get("candidate_id"))
            if row is None or d.get("decision") not in DECISIONS:
                continue
            decision, label = d["decision"], d["decision"]
            if decision == "at":
                at = self.agenda.parse(d.get("at") or "")
                if at is None or at <= self.schedule.now():
                    decision, label = "now", "now"
                else:
                    await self.outbox.set_tier(row["id"], "at")
                    await self.agenda.schedule_findings([row["id"]], at)
                    label = f"planerad {self.agenda.show(at)}"
            if decision == "now":
                await self.outbox.set_tier(row["id"], "now")
                now_rows.append(row)
                label = "nu"
            elif decision == "shelf":
                await self.outbox.set_tier(row["id"], "shelf", "shelved")
                label = "sparad till senare"
            elif decision == "drop":
                await self.outbox.set_tier(row["id"], "drop", "dropped")
                label = "släppt"
            await self.outbox.record(row["id"], MODULE, label, d.get("reason", ""), trace)
            to = {"at": "calendar", "now": "assistant", "shelf": "board", "drop": "bin"}[decision]
            await trail.emit("place", "manager", to=to, ref=f"#F{row['id']}")
        if now_rows:
            await self._deliver_findings(now_rows, self.schedule.now(), decided_now=True)

    async def _context(self, pending: list, avail) -> dict:
        now = self.schedule.now()
        gaps = await self.schedule.free_windows(now)
        candidates = []
        for r in pending:
            age = now - datetime.fromisoformat(r["created_at"] + "+00:00").astimezone(now.tzinfo)
            candidates.append(
                f"[id {r['id']}] {r['kind']} · {r['title']}\n"
                f"features: {r['features']}\n"
                f"waiting: {int(age.total_seconds() // 3600)} h · expires: {r['expires_at'] or '-'}\n"
                f"brief: {r['body']}"
            )
        modes = []
        if until := await self.silenced_until():
            modes.append(f"the owner asked for quiet until {self.agenda.show(until)}")
        if until := await self.focus_until():
            modes.append(f"the owner asked for only important things until {self.agenda.show(until)}")
        return {
            "now": f"{WEEKDAYS[now.weekday()]} {now:%Y-%m-%d %H:%M}",
            "availability": avail.status_line() + ("; " + "; ".join(modes) if modes else ""),
            "gaps": ", ".join(f"{a:%H:%M}–{b:%H:%M}" for a, b in gaps) or "none left today",
            "activity": await self._activity(),
            "interrupt": self.memory.section("profile.md", "får alltid störa") or "(empty)",
            "policy": self.memory.read("policy.md"),
            "planned": await self.agenda.upcoming(days=3),
            "candidates": "\n\n".join(candidates),
        }

    # --- delivering: Python, every minute ---------------------------------------

    async def tick(self) -> None:
        """Delivers what is due after a last check; retries undecided findings; offers an idle moment for a question."""
        async with self._lock:
            await self._prepare()
            await self.outbox.expire_shelf()
            now = self.schedule.now()
            if due := await self.agenda.due_findings(now):
                await self._deliver_findings(due, now)
            for b in await self.agenda.due_bookings(now):
                await self._deliver_booking(b, now)
            undecided = await self.outbox.pending()
        if undecided and (self._last_decide is None or now - self._last_decide >= RETRY_UNDECIDED):
            await self.run()
        avail = await self.schedule.availability(now)
        idle = not avail.quiet and avail.current is None and not await self.silenced_until() and not await self.focus_until()
        if idle and (self._last_ask is None or now - self._last_ask >= ASK_EVERY):
            self._last_ask = now
            await self._maybe_ask()

    async def _hold_until(self, now: datetime, urgent: bool, decided_now: bool = False) -> datetime | None:
        """When a delivery due now must wait instead (mechanics executing the plan, not new judgement). Quiet hours and
        the owner's own quiet or focus requests always hold; a session holds unless the dispatcher chose to interrupt."""
        if self.settings.quiet_hours.contains(now.time()):
            return self.agenda.quiet_end(now)
        if until := await self.silenced_until():
            return until
        if not urgent and (until := await self.focus_until()):
            return until
        if not (urgent or decided_now) and (busy := (await self.schedule.availability(now)).current) is not None:
            return busy.end + timedelta(minutes=2)
        return None

    async def _deliver_findings(self, rows: list, now: datetime, decided_now: bool = False) -> None:
        hold, go = [], []
        for r in rows:
            urgent = json.loads(r["features"] or "{}").get("time_sensitivity") == "high"
            until = await self._hold_until(now, urgent, decided_now)
            (hold if until else go).append((r, until))
        for r, until in hold:
            await self.outbox.set_tier(r["id"], "at")
            await self.agenda.schedule_findings([r["id"]], until)
            await self.outbox.record(r["id"], MODULE, f"väntar till {self.agenda.show(until)}", "Tiden kom, men det var nattro, paus eller ett pass.", {})
            await trail.emit("place", "manager", to="calendar", ref=f"#F{r['id']}")
        if go:
            await self.outbox.deliver([r for r, _ in go])  # the Assistant writes the message
            for r, _ in go:   # a planned one is taken from the calendar, one sent now from the Assistant's inbox
                await trail.emit("deliver", "assistant", source="inbox" if decided_now else "calendar", ref=f"#F{r['id']}")

    async def _deliver_booking(self, b, now: datetime) -> None:
        """A reminder the owner asked for, or a promise the Assistant made, through the one voice."""
        if not b["fixed"] and (until := await self._hold_until(now, urgent=False)):  # promises wait; the owner's own bookings don't
            await self.db.execute("UPDATE bookings SET due_at = ? WHERE id = ?", (utc_iso(until), b["id"]))
            return
        if not await self.db.execute_count("UPDATE bookings SET status = 'sending' WHERE id = ? AND status = 'planned'", (b["id"],)):
            return
        what = "a reminder the owner asked for" if b["kind"] == "reminder" else "something you promised the owner"
        ids: list[int] = []
        if self.outbox.speak is None:   # no chat at all (tests, a bare engine): the owner's own words are all there is
            ids = await self.outbox.send_text(b["body"])
        else:
            try:
                ids = await self.outbox.speak(
                    f"It is time for {what} (#B{b['id']}" + (f", about {b['ref']}" if b["ref"] else "") + f"): {b['body']}\n"
                    "Tell them now, briefly and in your own words; read what it is about if you need the details.",
                    about={f"#B{b['id']}"} | ({b["ref"]} if b["ref"] else set()),
                )
            except AlreadySaid:   # already said in the conversation: done, and nothing more goes out
                await self.db.execute("UPDATE bookings SET status = 'sent', sent_at = datetime('now'), reason = 'redan sagt i samtalet' WHERE id = ?", (b["id"],))
                return
            except Exception as e:
                log.warning("couldn't deliver booking %s through the chat: %s", b["id"], e)
            if not ids:   # never the booking's raw text: it is a note for the office. Try again soon, a few times.
                tries = (await self.db.kv_get(f"booking.tries.{b['id']}", 0)) + 1
                await self.db.kv_set(f"booking.tries.{b['id']}", tries)
                status = "planned" if tries < 4 else "failed"
                await self.db.execute("UPDATE bookings SET status = ?, due_at = ? WHERE id = ?",
                                      (status, utc_iso(now + timedelta(minutes=10)), b["id"]))
                log.warning("booking %s not delivered (try %d)%s", b["id"], tries, "; given up" if status == "failed" else "")
                return
        await self.db.execute(
            "UPDATE bookings SET status = 'sent', sent_at = datetime('now'), message_id = ? WHERE id = ?", (ids[0] if ids else None, b["id"])
        )
        await trail.emit("deliver", "assistant", source="calendar", ref=f"#B{b['id']}")
        from agent import refs

        await refs.link(self.db, ids, {f"#B{b['id']}"} | ({b["ref"]} if b["ref"] else set()))

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
            if self.outbox.speak is None:   # no chat at all (tests, a bare engine)
                poll_id, message_row_id = await self.send_poll(question["question"], question["options"])
            else:   # the one voice asks, as part of the conversation, and the chat records the answer (22 Oct: a poll came out of nowhere)
                sessions = "\n".join(f"- {o} (series {i})" for o, i in zip(question["options"], question["series_ids"]))
                try:
                    ids = await self.outbox.speak(
                        "Ask the owner which of these sessions in their timetable apply to them: briefly, as a question in your "
                        "conversation, with a few words on why (so you know when they are busy). When they answer, record it with "
                        "set_series_relevance for each series (mine, not_mine or sometimes).\n" + sessions,
                        about={f"TimeEdit-serie {i}" for i in question["series_ids"]})
                except Exception as e:
                    log.warning("couldn't ask about the schedule: %s", e)
                    return
                poll_id, message_row_id = f"chat:{ids[0] if ids else 0}", ids[0] if ids else None
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
        q = await self.inbox.open_question()
        if q is None:
            return
        try:
            ids = await self.outbox.speak(
                f"{q['sender'].capitalize()} wants to know something (#P{q['id']}). Ask the owner naturally and briefly, as "
                f"part of your conversation, not as a form. When they answer later, use `question` with #P{q['id']} and their words.\n"
                f"What: {q['body']}\nWhy (for you; no need to say it): {q['context'] or '-'}",
                about={f"#P{q['id']}"},
            )
        except Exception as e:
            log.warning("couldn't ask question %s: %s", q["id"], e)
            return
        await self.inbox.asked(q["id"], ids[0] if ids else None)
        log.info("asked question %s from %s", q["id"], q["sender"])

    async def _ask_manager(self, q: dict) -> None:
        if q["type"] == "proposal":
            message_row_id = await self.send_proposal(q["text"], q["id"])
        elif self.outbox.speak is not None:   # a check-in is a question in the conversation; Manager reads the answer there
            try:
                ids = await self.outbox.speak(
                    "Manager wants a short check-in. Ask the owner, in your own words and as part of the conversation: "
                    f"{q['question']} (the kinds of answer that help: {', '.join(q['options'])}). Nothing else.")
            except Exception as e:
                log.warning("couldn't ask the check-in: %s", e)
                return
            message_row_id = ids[0] if ids else None
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
