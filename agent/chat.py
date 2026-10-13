"""The persistent chat session with användaren: the agent's only voice (docs/ARCHITECTURE.md).

the owner's messages that arrive while Claude is busy are buffered and sent together as one turn.
Proactive messages are written here too: the dispatcher calls `speak()` with an internal instruction,
so everything the agent says lives in one conversation with one style.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import Settings
from agent.db import Database
from agent.posts import Posts
from agent import refs as refs_mod
from agent.memory import MemoryRepo
from agent.pipeline.outbox import SKIP, AlreadySaid
from agent.prompts import personalize
from agent.pursuits import Pursuits
from agent.prompts import prompt as prompt_template
from agent.schedule import Schedule
from agent.tools import TurnContext, build_chat_tools, build_github_tools, build_mail_tools, build_web_tools

log = logging.getLogger(__name__)

SESSION_KEY = "chat.session_id"
SEEN_KEY = "chat.seen_message_id"  # highest messages.id already shown to the chat session
PROACTIVE_LIMIT = 5
WEEKDAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]
MONTHS = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]

# Tools for the chat session; paths are relative to workspace/ and further limited by
# workspace/.claude/settings.json (e.g. policy.md is read-only).
# Rights follow ownership (docs/MODEL.md): the Assistant reads all of memory but writes only its own files.
ASSISTANT_FILES = ("profile.md", "interests.md", "projects.md", "school.md", "schedule.md", "interrupt.md", "arbetsminne/assistant.md")
CHAT_TOOLS = [
    "Read(./memory/**)",
    *(f"{verb}(./memory/{f})" for f in ASSISTANT_FILES for verb in ("Edit", "Write")),
    "WebSearch",
]


@dataclass
class Incoming:
    text: str
    replied_to_message_id: int | None = None  # messages.id
    replied_to_text: str | None = None
    message_row_id: int | None = None  # messages.id of an incoming message, so the turn's IDs can be linked to it
    internal: bool = False  # an instruction from the system (dispatcher, Director, Manager), not from the owner
    done: asyncio.Future | None = None  # internal turns: resolved with the messages.id rows that were sent


Send = Callable[..., Awaitable[list[int]]]
Typing = Callable[[], Awaitable[None]]


class ChatSession:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        gate: ClaudeGate,
        memory: MemoryRepo,
        send: Send,
        typing: Typing,
        schedule: Schedule | None = None,
    ):
        self.settings = settings
        self.schedule = schedule
        self.controls = None  # set by main once the dispatcher exists
        self.builder = None  # set by main
        self.github = None  # set by main: read-only GitHubReader
        self.web = None  # set by main: WebReader (web_read tool)
        self.mail = None  # set by main: read-only MailReader
        self.db = db
        self.inbox = Posts(db)
        self.gate = gate
        self.memory = memory
        self._send = send
        self._typing = typing
        self._buffer: list[Incoming] = []
        self._worker: asyncio.Task | None = None

    def submit(self, msg: Incoming) -> None:
        self._buffer.append(msg)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain())

    async def speak(self, instruction: str) -> list[int]:
        """Have the session write and send a message itself. Raises if it couldn't (caller falls back)."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.submit(Incoming(text=instruction, internal=True, done=fut))
        return await fut

    async def start_interview(self) -> None:
        """For a new owner (empty memory). Later interviews start from the chat itself (the interview_guide tool)."""
        self.submit(Incoming(text=prompt_template("interview") + "\n\nBegin now with a short greeting and the first question.", internal=True))

    async def reset(self) -> None:
        await self.db.kv_set(SESSION_KEY, None)

    # --- internals --------------------------------------------------------

    async def _drain(self) -> None:
        while self._buffer:
            if self._buffer[0].internal:  # internal instructions get a turn of their own
                batch, self._buffer = [self._buffer[0]], self._buffer[1:]
            else:  # consecutive messages from the owner are answered together
                n = next((i for i, m in enumerate(self._buffer) if m.internal), len(self._buffer))
                batch, self._buffer = self._buffer[:n], self._buffer[n:]
            try:
                await self._turn(batch)
            except Exception as e:
                log.exception("chat turn failed")
                if batch[0].internal:
                    if batch[0].done and not batch[0].done.done():
                        batch[0].done.set_exception(e)
                else:
                    await self._send("Något gick fel på min sida. Försök igen om en stund.", origin="chat", in_session=True)

    def _now(self) -> str:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        return f"{WEEKDAYS[now.weekday()]} {now.day} {MONTHS[now.month - 1]} {now:%H:%M}"

    async def _prompt(self, batch: list[Incoming]) -> str:
        parts = [f"[time: {self._now()}]"]
        if self.schedule is not None:
            try:
                parts.append(f"[schedule: {(await self.schedule.availability()).status_line()}]")
            except Exception:
                log.exception("availability failed")
        if not await self.db.kv_get(SESSION_KEY) and (seed := await self.db.kv_get("chat.seed_summary")):
            parts.append(f"[new session: summary of the conversation so far]\n{seed}\n[end]")
            parts.append(await self._recent_conversation(limit=12))
        parts.extend(await self._proactive_since_last_turn())
        if not batch[0].internal and (q := await self.inbox.open_question()):
            parts.append(
                f"[open question #P{q['id']} from {q['sender']}: ask only if it fits naturally now; if you ask it, call "
                f"mark_question_asked. When {{owner}} answers, send_post with answer_to #P{q['id']}. "
                f"What: {q['body']} · Why: {q['context'] or '-'}]".replace("{owner}", "the owner")
            )
        for m in batch:
            if m.internal:
                parts.append(f"[internal task: not from the owner. Your reply is sent to them verbatim as a message from you]\n{m.text}")
                continue
            if m.replied_to_text:
                quoted = m.replied_to_text if len(m.replied_to_text) <= 300 else m.replied_to_text[:300] + "…"
                parts.append(f'[replying to: "{quoted}"]')
            parts.append(m.text)
        return "\n".join(parts)

    async def _recent_conversation(self, limit: int = 30) -> str:
        """Bridges a lost session (e.g. after moving machines) with the latest messages from the database."""
        rows = await self.db.fetchall(
            "SELECT direction, origin, text FROM messages WHERE text != '' ORDER BY id DESC LIMIT ?", (limit,)
        )
        if not rows:
            return ""
        lines = []
        for r in reversed(rows):
            who = "owner" if r["direction"] == "in" else ("you (proactively)" if r["origin"] == "proactive" else "you")
            text = r["text"] if len(r["text"]) <= 600 else r["text"][:600] + "…"
            lines.append(f"{who}: {text}")
        return "[new session: the latest messages before this session, for context]\n" + "\n".join(lines) + "\n[end]\n"

    async def _proactive_since_last_turn(self) -> list[str]:
        """Messages sent outside the session since the last turn (polls, proposals, fallback deliveries).

        The session didn't write those itself, so without this it wouldn't know about them."""
        seen = await self.db.kv_get(SEEN_KEY, 0)
        rows = await self.db.fetchall(
            "SELECT id, text, created_at FROM messages WHERE direction = 'out' AND COALESCE(origin, 'chat') != 'chat' "
            "AND in_session = 0 AND id > ? ORDER BY id",
            (seen,),
        )
        top = await self.db.fetchone("SELECT max(id) AS m FROM messages")
        await self.db.kv_set(SEEN_KEY, top["m"] or 0)
        out = []
        tz = ZoneInfo(self.settings.timezone)
        for r in rows[-PROACTIVE_LIMIT:]:
            when = datetime.fromisoformat(r["created_at"] + "+00:00").astimezone(tz)
            text = r["text"] if len(r["text"]) <= 1500 else r["text"][:1500] + "…"
            out.append(f"[you sent proactively {WEEKDAYS[when.weekday()]} {when:%H:%M}]\n{text}\n[end]")
        return out

    async def _turn(self, batch: list[Incoming]) -> None:
        turn = TurnContext(replied_to_message_id=next((m.replied_to_message_id for m in reversed(batch) if m.replied_to_message_id), None))
        mcp_servers, tool_names = build_chat_tools(
            self.db, turn, self.schedule, self.controls, self.builder, self.inbox, getattr(self.memory, "path", None)
        )
        if self.web is not None:
            w_servers, w_names = build_web_tools(self.web, turn)
            mcp_servers, tool_names = {**mcp_servers, **w_servers}, tool_names + w_names
        if self.github is not None:
            gh_servers, gh_names = build_github_tools(self.github)
            mcp_servers, tool_names = {**mcp_servers, **gh_servers}, tool_names + gh_names
        if self.mail is not None:
            m_servers, m_names = build_mail_tools(self.mail, turn)
            mcp_servers, tool_names = {**mcp_servers, **m_servers}, tool_names + m_names
        internal = batch[0].internal
        session_id = await self.db.kv_get(SESSION_KEY)
        prompt = await self._prompt(batch)
        pursuing = await Pursuits(self.db, self.settings.timezone).summary()
        working_memory = self.memory.read("arbetsminne/assistant.md")

        def task(resume: str | None, prefix: str = "") -> ClaudeTask:
            return ClaudeTask(
                kind="chat",
                prompt=prefix + prompt,
                priority=Priority.CHAT,
                model=self.settings.models.chat,
                resume=resume,
                allowed_tools=CHAT_TOOLS + tool_names,
                mcp_servers=mcp_servers,
                system_prompt_append=prompt_template("chat")
                + "\n\n# Style guide and current state (owned by Manager; follow them)\n"
                + self.memory.read("style.md", "state.md")
                + "\n\n# Interests, and what never to suggest (applies even to things discussed earlier)\n"
                + self.memory.read("interests.md")
                + "\n\n# What " + personalize("{owner}") + " is pursuing (keep it current with track_pursuit)\n"
                + pursuing
                + ("\n\n# Your working memory (from your last night pass)\n" + working_memory if working_memory else ""),
            )

        typing = None if internal else asyncio.create_task(self._keep_typing())
        try:
            result = await self.gate.run(task(session_id))
            if result.is_error and session_id:
                # The stored session may be gone (e.g. new machine); start a fresh one once.
                log.warning("chat resume failed (%s), starting a new session", result.error)
                result = await self.gate.run(task(None, await self._recent_conversation()))
        except QuotaPaused as e:
            if internal:
                raise
            await self._send(f"Min Claude-kvot är slut just nu. Den återställs {datetime.fromtimestamp(e.resets_at):%H:%M}.", origin="chat", in_session=True)
            return
        finally:
            if typing:
                typing.cancel()

        if result.is_error:
            if internal:
                raise RuntimeError(result.error or "chat error")
            await self._send(f"Det blev fel: {result.error or 'okänt'}", origin="chat", in_session=True)
            return

        if result.session_id:
            await self.db.kv_set(SESSION_KEY, result.session_id)
            await self.db.kv_set("chat.seed_summary", None)
        ids: list[int] = []
        if internal and result.text.strip() == SKIP:  # a delivery that would only repeat what was already said
            if batch[0].done and not batch[0].done.done():
                batch[0].done.set_exception(AlreadySaid())
            return
        if result.text.strip():
            ids = await self._send(result.text.strip(), origin="proactive" if internal else "chat", in_session=True)
        if turn.refs:  # link what this turn was about to its messages, so it can be looked up later
            await refs_mod.link(self.db, ids + [m.message_row_id for m in batch if m.message_row_id], turn.refs)
        if internal and batch[0].done and not batch[0].done.done():
            batch[0].done.set_result(ids)
        await self.memory.commit_if_changed(f"chat {self._now()}")

    async def _keep_typing(self) -> None:
        while True:
            try:
                await self._typing()
            except Exception:
                pass
            await asyncio.sleep(4.5)
