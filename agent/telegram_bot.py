"""Telegram interface. Only the owner's chat is served."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    PollAnswerHandler,
    filters,
)

from agent.chat import ChatSession, Incoming
from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import Settings
from agent.db import Database
from agent.memory import MemoryRepo
from agent.schedule import Schedule

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096


class TelegramBot:
    def __init__(self, settings: Settings, db: Database, gate: ClaudeGate, memory: MemoryRepo, schedule: Schedule):
        self.settings = settings
        self.db = db
        self.gate = gate
        self.memory = memory
        self.schedule = schedule
        self.started_at = time.time()
        self.app = Application.builder().token(settings.telegram_token).build()
        self.chat = ChatSession(settings, db, gate, memory, send=self.send, typing=self._typing, schedule=schedule)
        self.controls = None  # set by main (Controls needs the dispatcher, which needs this bot)
        self.builder = None  # set by main
        self.manager = None  # set by main
        self._background: set[asyncio.Task] = set()
        self._register()

    def _register(self) -> None:
        # Before the chat id is configured, /start just tells the user their id.
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        if self.settings.telegram_chat_id is None:
            log.warning("TELEGRAM_CHAT_ID not set: only /start is answered")
            return

        owner = filters.Chat(chat_id=self.settings.telegram_chat_id)
        self.app.add_handler(CommandHandler("ping", self.cmd_ping, filters=owner))
        self.app.add_handler(CommandHandler("status", self.cmd_status, filters=owner))
        self.app.add_handler(CommandHandler("claude", self.cmd_claude, filters=owner))
        self.app.add_handler(CommandHandler("ny", self.cmd_new_session, filters=owner))
        self.app.add_handler(CommandHandler("schema", self.cmd_schedule, filters=owner))
        self.app.add_handler(CommandHandler("tyst", self.cmd_silence, filters=owner))
        self.app.add_handler(CommandHandler("fokus", self.cmd_focus, filters=owner))
        self.app.add_handler(CommandHandler("nytt", self.cmd_shelf, filters=owner))
        self.app.add_handler(CommandHandler("varfor", self.cmd_why, filters=owner))
        self.app.add_handler(CommandHandler("sok", self.cmd_research, filters=owner))
        self.app.add_handler(CommandHandler("policy", self.cmd_policy, filters=owner))
        self.app.add_handler(CommandHandler("kallor", self.cmd_sources, filters=owner))
        self.app.add_handler(CommandHandler("manager", self.cmd_manager, filters=owner))
        self.app.add_handler(CallbackQueryHandler(self.on_proposal_button, pattern=r"^proposal:"))
        self.app.add_handler(CallbackQueryHandler(self.on_change_button, pattern=r"^change:"))
        self.app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, self.on_text))
        self.app.add_handler(MessageReactionHandler(self.on_reaction, chat_id=self.settings.telegram_chat_id))
        self.app.add_handler(PollAnswerHandler(self.on_poll_answer))

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
        log.info("telegram polling started")

    async def stop(self) -> None:
        if self.app.updater.running:
            await self.app.updater.stop()
        if self.app.running:
            await self.app.stop()
        await self.app.shutdown()

    async def send(self, text: str, candidate_id: int | None = None, origin: str = "proactive", in_session: bool = False) -> list[int]:
        """Sends to the owner and logs each outgoing message, so replies and reactions can be traced back.

        Returns the messages.id rows (one per chunk)."""
        if self.settings.telegram_chat_id is None:
            return []
        ids = []
        for chunk in _chunks(text):
            msg = await self.app.bot.send_message(self.settings.telegram_chat_id, chunk, disable_web_page_preview=True)
            ids.append(
                await self.db.execute(
                    "INSERT INTO messages (telegram_message_id, direction, candidate_id, text, origin, in_session) VALUES (?, 'out', ?, ?, ?, ?)",
                    (msg.message_id, candidate_id, chunk, origin, int(in_session)),
                )
            )
        return ids

    async def send_poll(self, question: str, options: list[str], multiple: bool = True) -> tuple[str, int | None]:
        """Non-anonymous so poll answers reach the bot. Returns (poll_id, messages.id)."""
        msg = await self.app.bot.send_poll(
            self.settings.telegram_chat_id, question, options, is_anonymous=False, allows_multiple_answers=multiple
        )
        row_id = await self.db.execute(
            "INSERT INTO messages (telegram_message_id, direction, text, origin) VALUES (?, 'out', ?, 'proactive')",
            (msg.message_id, "[omröstning] " + question + "\n" + "\n".join(f"- {o}" for o in options)),
        )
        return msg.poll.id, row_id

    async def send_proposal(self, text: str, proposal_id: int) -> int:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Ja", callback_data=f"proposal:{proposal_id}:yes"),
            InlineKeyboardButton("Nej", callback_data=f"proposal:{proposal_id}:no"),
        ]])
        msg = await self.app.bot.send_message(self.settings.telegram_chat_id, text, reply_markup=keyboard)
        return await self.db.execute(
            "INSERT INTO messages (telegram_message_id, direction, text, origin) VALUES (?, 'out', ?, 'proactive')",
            (msg.message_id, text),
        )

    async def on_proposal_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q.from_user is None or q.from_user.id != self.settings.telegram_chat_id:
            await q.answer()
            return
        _, pid, answer = q.data.split(":")
        reply = await self.manager.decide_proposal(int(pid), answer == "yes")
        await q.answer()
        await q.edit_message_text(f"{q.message.text}\n\n→ {'Ja' if answer == 'yes' else 'Nej'}. {reply}")

    async def send_change(self, change_id: int) -> None:
        """Build finished: summary, what changed, test result, and the decision buttons."""
        c = await self.db.fetchone("SELECT * FROM changes WHERE id = ?", (change_id,))
        risk = {"low": "låg", "medium": "medel", "high": "hög"}.get(c["risk"] or "", "?")
        if c["status"] == "ready":
            text = (
                f"Ändring klar: {c['title']}\n\n{c['summary']}\n\n"
                f"Risk: {risk} · Tester: gröna\n{c['diffstat'] or ''}"
            )
            buttons = [[
                InlineKeyboardButton("Driftsätt", callback_data=f"change:{change_id}:deploy"),
                InlineKeyboardButton("Släng", callback_data=f"change:{change_id}:discard"),
            ], [InlineKeyboardButton("Visa ändringen", callback_data=f"change:{change_id}:diff")]]
        else:
            tail = (c["test_output"] or "").strip().splitlines()[-12:]
            text = f"Ändringen gick inte igenom: {c['title']}\n\n{c['summary'] or ''}" + ("\n\nTester:\n" + "\n".join(tail) if tail else "")
            buttons = [[InlineKeyboardButton("Släng", callback_data=f"change:{change_id}:discard")]] if c["status"] == "failed" and c["diffstat"] else []
        msg = await self.app.bot.send_message(
            self.settings.telegram_chat_id, text[:4000], reply_markup=InlineKeyboardMarkup(buttons) if buttons else None
        )
        await self.db.execute(
            "INSERT INTO messages (telegram_message_id, direction, text, origin) VALUES (?, 'out', ?, 'proactive')", (msg.message_id, text)
        )

    async def on_change_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        if q.from_user is None or q.from_user.id != self.settings.telegram_chat_id:
            return
        _, cid, action = q.data.split(":")
        if action == "diff":
            diff = await self.builder.diff(int(cid))
            if not diff:
                await self.send("Hittar inte ändringen längre.", origin="command")
                return
            await self.app.bot.send_document(
                self.settings.telegram_chat_id, document=diff.encode(), filename=f"andring-{cid}.diff"
            )
            return
        await q.edit_message_reply_markup(reply_markup=None)
        reply = await (self.builder.deploy(int(cid)) if action == "deploy" else self.builder.discard(int(cid)))
        await self.send(reply, origin="command")

    async def cmd_policy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = self.manager.memory.read("policy.md").replace("--- policy.md ---\n", "")
        await self.send(text or "Ingen policy än.", origin="command")

    async def cmd_manager(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Kör Manager nu. Det tar en stund.")

        async def go() -> None:
            out = await self.manager.run()
            if out is None:
                await self.send("Manager kunde inte köras just nu, se loggen.", origin="command")
            else:
                await self.send("Manager klar.\n\n" + (out.get("changelog") or "Inga ändringar."), origin="command")

        task = asyncio.create_task(go())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _typing(self) -> None:
        await self.app.bot.send_chat_action(self.settings.telegram_chat_id, ChatAction.TYPING)

    async def _message_row_id(self, telegram_message_id: int) -> int | None:
        row = await self.db.fetchone(
            "SELECT id FROM messages WHERE telegram_message_id = ? ORDER BY id DESC LIMIT 1", (telegram_message_id,)
        )
        return row["id"] if row else None

    # --- handlers ---------------------------------------------------------

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if self.settings.telegram_chat_id is None:
            await update.message.reply_text(
                f"Hej! Ditt chat-id är {chat_id}.\n"
                "Lägg in det som TELEGRAM_CHAT_ID i .env och starta om agenten."
            )
        elif chat_id == self.settings.telegram_chat_id and self.memory.is_new():
            await self.chat.start_interview()  # a new owner: get to know them first
        elif chat_id == self.settings.telegram_chat_id:
            await update.message.reply_text(
                "Hej! Jag är igång. Skriv till mig som vanligt, du behöver inga kommandon: "
                "\"var tyst ett tag\", \"något nytt?\", \"varför skickade du det?\" fungerar.\n\nGenvägar om du vill:\n"
                "/ny – börja en ny konversation (minnet finns kvar)\n"
                "/schema – idag och imorgon\n/nytt – hyllan: bra saker jag inte störde dig med\n"
                "/tyst [2h] – inga utskick ett tag\n/fokus [2h] – bara sånt som får störa\n"
                "/varfor – svara på ett meddelande från mig för att se varför jag skickade det\n"
                "/sok – kör en undersökning nu\n/policy – hur jag just nu tänker kring tempo och innehåll\n/kallor – hur mina källor presterar\n"
                "/status – hur jag mår\n/ping"
            )

    async def cmd_ping(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        uptime = int(time.time() - self.started_at)
        await update.message.reply_text(f"pong · uppe {uptime // 3600}h {uptime % 3600 // 60}m")

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        row = await self.db.fetchone(
            "SELECT count(*) AS n, sum(is_error) AS errors, sum(cost_usd) AS cost "
            "FROM claude_runs WHERE created_at >= datetime('now', '-1 day')"
        )
        s = self.gate.status()
        lines = [
            "<b>Status</b>",
            f"Claude: {'upptagen' if s['busy'] else 'ledig'}, {s['waiting']} i kö",
            f"Körningar senaste dygnet: {row['n']} ({row['errors'] or 0} fel)",
        ]
        if row["cost"]:
            lines.append(f"Beräknad kostnad (API-ekvivalent): ${row['cost']:.2f}")
        if rl := s["rate_limit"]:
            util = f"{rl['utilization']:.0%}" if rl["utilization"] is not None else "?"
            lines.append(f"Kvot: {rl['status']} ({rl['type']}, {util})")
        if s["paused_until"]:
            lines.append(f"Bakgrundsjobb pausade (≥ {s['paused_from']}) till {s['paused_until']}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_claude(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Debug: one-shot Claude run through the gate, no tools, no memory."""
        prompt = " ".join(ctx.args or [])
        if not prompt:
            await update.message.reply_text("Användning: /claude <fråga>")
            return
        await update.effective_chat.send_action(ChatAction.TYPING)
        try:
            r = await self.gate.run(
                ClaudeTask(kind="debug", prompt=prompt, priority=Priority.CHAT, model=self.settings.models.chat, max_turns=1)
            )
        except QuotaPaused as e:
            await update.message.reply_text(f"Kvoten är slut till {time.ctime(e.resets_at)}.")
            return
        if r.is_error:
            await update.message.reply_text(f"Fel: {r.error or 'okänt'}")
            return
        footer = f"\n\n· {r.duration_ms / 1000:.1f}s" if r.duration_ms else ""
        for chunk in _chunks(r.text + footer):
            await update.message.reply_text(chunk)

    async def cmd_new_session(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.chat.reset()
        await update.message.reply_text("Ny konversation. Allt i minnet finns kvar.")

    async def cmd_schedule(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        avail = await self.schedule.availability()
        days = await self.schedule.format_days(2)
        unknown = len(await self.schedule.unknown_series())
        tail = f"\n\n[?] = vet inte om det gäller dig ({unknown} sorters pass okända)." if unknown else ""
        await update.message.reply_text(f"{avail.status_line()}\n\n{days}{tail}")

    async def cmd_silence(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._pause(update, ctx, "quiet")

    async def cmd_focus(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._pause(update, ctx, "focus")

    async def _pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
        arg = " ".join(ctx.args or []).strip().lower()
        if arg in ("av", "off", "stop", "0"):
            minutes = 0
        else:
            duration = _parse_duration(arg) if arg else timedelta(hours=3)
            if duration is None:
                await update.message.reply_text("Ange t.ex. 30m, 2h, 1h30m eller 'av'. Du kan också bara skriva det till mig.")
                return
            minutes = int(duration.total_seconds() // 60)
        await update.message.reply_text(await self.controls.pause(mode, minutes))

    async def cmd_shelf(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text, rows = await self.controls.shelf()
        ids = await self.send(text, origin="command")
        for mid in ids:
            for r in rows:
                await self.db.execute("INSERT OR IGNORE INTO message_candidates (message_id, candidate_id) VALUES (?, ?)", (mid, r["id"]))

    async def cmd_research(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        arg = (ctx.args or [""])[0]
        if arg not in self.controls.research_ids():
            await update.message.reply_text("Vilken undersökning?\n" + "\n".join(f"/sok {i}" for i in self.controls.research_ids()))
            return
        await update.message.reply_text(await self.controls.start_research(arg))

    async def cmd_why(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        replied = update.message.reply_to_message
        mid = await self._message_row_id(replied.message_id) if replied else await self.controls.last_proactive_message_id()
        await update.message.reply_text((await self.controls.why(mid))[:4000])

    async def cmd_sources(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self.controls.sources())

    async def on_poll_answer(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        answer = update.poll_answer
        if answer.user is None or answer.user.id != self.settings.telegram_chat_id:
            return
        row = await self.db.fetchone("SELECT kind, payload FROM polls WHERE poll_id = ?", (answer.poll_id,))
        if row is not None and row["kind"] == "checkin":
            options = json.loads(row["payload"])["options"]
            chosen = [options[i] for i in answer.option_ids if i < len(options)]
            if chosen:
                await self.db.execute("UPDATE polls SET answer = ?, answered_at = datetime('now') WHERE poll_id = ?", (json.dumps(chosen, ensure_ascii=False), answer.poll_id))
                await self.db.execute("INSERT INTO feedback (signal, value) VALUES ('poll', ?)", (f"checkin:{chosen[0]}",))
                await self.send("Tack, det tar jag med mig.", origin="command")
            return
        reply = await self.schedule.handle_poll_answer(answer.poll_id, list(answer.option_ids))
        if reply:
            await self.send(reply)

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        replied_row = replied_text = None
        if msg.reply_to_message:
            replied_text = msg.reply_to_message.text
            replied_row = await self._message_row_id(msg.reply_to_message.message_id)
        await self.db.execute(
            "INSERT INTO messages (telegram_message_id, direction, text) VALUES (?, 'in', ?)", (msg.message_id, msg.text)
        )
        if replied_row is not None:
            await self.db.execute(
                "INSERT INTO feedback (message_id, candidate_id, signal, value, note) "
                "SELECT id, candidate_id, 'reply', 'reply', ? FROM messages WHERE id = ?",
                (msg.text[:500], replied_row),
            )
        self.chat.submit(Incoming(text=msg.text, replied_to_message_id=replied_row, replied_to_text=replied_text))

    async def on_reaction(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        r = update.message_reaction
        row_id = await self._message_row_id(r.message_id)
        emojis = [getattr(x, "emoji", None) or x.type for x in r.new_reaction]
        value = " ".join(emojis) if emojis else "removed"
        await self.db.execute(
            "INSERT INTO feedback (message_id, candidate_id, signal, value) "
            "VALUES (?, (SELECT candidate_id FROM messages WHERE id = ?), 'reaction', ?)",
            (row_id, row_id, value),
        )
        log.info("reaction on message %s: %s", r.message_id, value)


def _parse_duration(arg: str) -> timedelta | None:
    m = re.fullmatch(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?", arg.replace(" ", ""))
    if not m or not (m.group(1) or m.group(2)):
        return None
    return timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))


def _chunks(text: str, size: int = TELEGRAM_LIMIT) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]
