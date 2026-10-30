"""The persistent chat session with användaren: the agent's only voice (docs/MODEL.md).

the owner's messages that arrive while Claude is busy are buffered and sent together as one turn.
Proactive messages are written here too: the dispatcher calls `speak()` with an internal instruction,
so everything the agent says lives in one conversation with one style.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from agent.search import search


def question_brief(q: dict) -> str:
    """What the Assistant needs to ask a role's question well and follow up itself: why, the guess, what would settle it."""
    return (f"What: {q['title']} · Why it matters: {q.get('why') or '-'}" + (f" · Their guess: {q['guess']}" if q.get("guess") else "")
            + (f" · Settled when: {q['settles_it']}" if q.get("settles_it") else "") + (f" · Context: {q['context']}" if q.get("context") else ""))


async def in_progress(db) -> list[dict]:
    """Questions to the owner that have been asked and not yet recorded: the Assistant is still finding out."""
    from agent.items import Items

    return await Items(db).find("question", status="asked", where="json_extract(data, '$.to') = 'owner' AND json_extract(data, '$.asked_at') >= ?",
                                args=(f"{datetime.now() - timedelta(days=3):%Y-%m-%dT%H:%M}",), order="id", limit=3)


async def open_item_question(db) -> dict | None:
    """The oldest question for the owner from a role (#Q), unless one is already waiting for an answer."""
    from agent.items import Items

    items = Items(db)
    if await items.find("question", status="asked", where="json_extract(data, '$.to') = 'owner' AND json_extract(data, '$.asked_at') >= ?",
                        args=(f"{datetime.now() - timedelta(hours=24):%Y-%m-%dT%H:%M}",), limit=1):
        return None   # one just asked: let it be answered first
    found = await items.find("question", status="open", where="json_extract(data, '$.to') = 'owner'", limit=1)
    return found[0] if found else None
from agent.tools import TurnContext, build_chat_tools, build_github_tools, build_mail_tools, build_web_tools, create_sdk_mcp_server

log = logging.getLogger(__name__)

SESSION_KEY = "chat.session_id"
SEEN_KEY = "chat.seen_message_id"  # highest messages.id already shown to the chat session
PROACTIVE_LIMIT = 5
RECALL = 4    # search hits added before an owner's turn
RECENT = 20   # the latest messages are in the conversation already: recall looks further back
DELIVER_CONTEXT = 12   # messages a delivery reads
WINDOW_TOKENS = 30_000   # past this, a quiet conversation is folded into its summary (a routine may change it)
IDLE_MINUTES = 45
SUMMARY_KEY = "chat.summary"          # the running summary of the conversation
SUMMARY_UPTO_KEY = "chat.summary_upto"   # the last messages.id it covers
SEED_KEY = "chat.seed_summary"        # what the next new session starts from
# What the owner reads is only this field: never the text the session writes between its tool calls.
REPLY = {
    "type": "object",
    "properties": {"message": {"type": "string", "description": "Swedish. Exactly the message the owner reads, nothing else. "
                                                                 "Empty for an internal task that would only repeat what you already said."}},
    "required": ["message"],
}
WEEKDAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]
MONTHS = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]

# Tools for the chat session; paths are relative to workspace/. The chat reads all of memory and writes none of it:
# what the owner says becomes beliefs (`remember`), and the Assistant's working memory is tidied in its night pass,
# a section at a time (agent/texts.py).
CHAT_TOOLS = [
    "Read(./memory/**)",
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
    deliver: bool = False  # written by the deliver skill, with a short brief, outside the long conversation
    about: frozenset[str] = frozenset()  # ids a delivery is about, linked to the message it becomes


Send = Callable[..., Awaitable[list[int]]]
Typing = Callable[[], Awaitable[None]]


def failure_words(error: str | None) -> str:
    """What the owner reads when a turn failed: in their world, never the error itself."""
    import re

    text = error or ""
    if re.search(r"(session|usage|weekly|rate) limit", text, re.I):
        m = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I)
        if m:
            hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
            return f"Min Claude-kvot är slut just nu. Den återställs {hour:02d}:{m.group(2) or '00'}, skriv igen då."
        return "Min Claude-kvot är slut just nu. Skriv igen om en stund."
    return "Något gick fel på min sida. Försök igen om en stund."


def _first_row(batch: list[Incoming]) -> int | None:
    rows = [m.message_row_id for m in batch if m.message_row_id]
    return min(rows) if rows else None


def message_of(result) -> str:
    """The message the owner reads: the reply field. Without one (a run that ended oddly), only text that can't be
    narration between tool calls: all of it when no tool was used, else what came after the last tool call."""
    if isinstance(result.structured, dict) and isinstance(result.structured.get("message"), str):
        return result.structured["message"].strip()
    if result.structured is not None or result.trace:
        log.warning("chat reply without its message field; using only the text after the last tool call")
        return (result.final_text or "").strip()
    return (result.text or "").strip()


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

    async def speak(self, instruction: str, about: set[str] | frozenset[str] = frozenset()) -> list[int]:
        """The office has something for the owner: the deliver skill writes it, in the same queue as the conversation
        so it sees what was just said, and sends it. Raises AlreadySaid when the owner already has all of it, and
        another error when nothing could be written (the caller tries again later; never a raw draft)."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.submit(Incoming(text=instruction, internal=True, done=fut, deliver=True, about=frozenset(about)))
        return await fut

    async def start_interview(self) -> None:
        """For a new owner (empty memory). Later interviews start from the chat itself (the interview_guide tool)."""
        host = (self.settings.host or "").strip()
        of = host if host[-1:].lower() in ("s", "x", "z") else host + "s"   # Swedish genitive: "Julius dator", "Annas dator"
        he = self.settings.host_pronoun or ""
        where = (f"\n\nThis Audax runs on {of} computer and Claude subscription, so that the owner can try it. Say it in the opening, "
                 f"in about these words: \"Det körs på {of} dator och Claude-abonnemang, så det du skriver sparas där och {host} kan "
                 f"läsa det. Du bestämmer själv vad du delar.\" "
                 + (f"{host}'s pronoun is \"{he}\"." if he else f"Refer to {host} by name, never with a guessed pronoun.") if host else "")
        self.submit(Incoming(text=prompt_template("interview") + where + "\n\nBegin now: the hello, what Audax is, the office, and the names.",
                             internal=True))

    async def reset(self) -> None:
        await self.db.kv_set(SESSION_KEY, None)

    async def tidy(self) -> bool:
        """The window (instance/granskning/2026-10-20-omgang-4-rosten.md, 4.2): a conversation that has grown past its
        size and been quiet a while is folded into its summary, and the next message starts a fresh session from it, the
        latest messages and what search finds. Quiet, because the cache has gone cold by then anyway, so starting over
        costs nothing extra. How big and how quiet are a routine the roles may change ({"skill": "chat", ...})."""
        from agent import routines

        if not await self.db.kv_get(SESSION_KEY) or (self._worker and not self._worker.done()):
            return False
        last = await self.db.fetchone("SELECT context_tokens FROM claude_runs WHERE kind = 'chat' AND context_tokens IS NOT NULL ORDER BY id DESC LIMIT 1")
        if not last or last["context_tokens"] < routines.cadence("chat", "window_tokens", WINDOW_TOKENS):
            return False
        idle = routines.cadence("chat", "idle_minutes", IDLE_MINUTES)
        if await self.db.fetchone("SELECT 1 FROM messages WHERE created_at >= datetime('now', ?)", (f"-{idle:g} minutes",)):
            return False
        if not await self.fold():
            return False
        await self.db.kv_set(SESSION_KEY, None)
        log.info("chat: the conversation (%d tokens) was folded into its summary; the next message starts fresh", last["context_tokens"])
        return True

    async def fold(self) -> bool:
        """The summary so far plus what was said since it, folded into a new one (cheap model, no tools)."""
        upto = await self.db.kv_get(SUMMARY_UPTO_KEY, 0)
        rows = await self.db.fetchall("SELECT id, direction, origin, text, created_at FROM messages WHERE id > ? AND text != '' ORDER BY id",
                                      (upto,))
        if not rows:
            return True
        tz = ZoneInfo(self.settings.timezone)
        lines = []
        for r in rows[-150:]:
            when = datetime.fromisoformat(r["created_at"] + "+00:00").astimezone(tz)
            who = "owner" if r["direction"] == "in" else ("assistant (on its own)" if r["origin"] == "proactive" else "assistant")
            text = r["text"] if len(r["text"]) <= 800 else r["text"][:800] + "…"
            lines.append(f"{when:%d/%m %H:%M} {who}: {text}")
        try:
            result = await self.gate.run(ClaudeTask(
                kind="chat_summary",
                prompt=prompt_template("chat_summary").format(previous=await self.db.kv_get(SUMMARY_KEY) or "(none yet)",
                                                              conversation="\n".join(lines)),
                priority=Priority.CHAT,
                model=self.settings.models.triage,
                output_schema={"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
                max_turns=2,
            ))
        except QuotaPaused:
            return False
        summary = (result.structured or {}).get("summary", "").strip() if isinstance(result.structured, dict) else ""
        if result.is_error or not summary:
            log.warning("chat summary failed: %s", result.error)
            return False
        await self.keep_summary(summary, rows[-1]["id"])
        return True

    async def keep_summary(self, summary: str, upto: int) -> None:
        """The summary a new session starts from, and how far into the conversation it reaches (the night pass too)."""
        await self.db.kv_set(SUMMARY_KEY, summary)
        await self.db.kv_set(SUMMARY_UPTO_KEY, upto)
        await self.db.kv_set(SEED_KEY, summary)

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
            except AlreadySaid as e:   # a delivery the owner already has: nothing is sent, and that is fine
                if batch[0].done and not batch[0].done.done():
                    batch[0].done.set_exception(e)
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
        if line := await self._schedule_line():
            parts.append(line)
        if not await self.db.kv_get(SESSION_KEY) and (seed := await self.db.kv_get(SEED_KEY)):
            parts.append(f"[new session: summary of the conversation so far]\n{seed}\n[end]")
            parts.append(await self._recent_conversation(limit=12, before=_first_row(batch)))
        parts.extend(await self._proactive_since_last_turn())
        if not batch[0].internal and (q := await self.inbox.open_question()):
            parts.append(
                f"[open question #P{q['id']} from {q['sender']}: ask only if it fits naturally now, in your own words; if you "
                f"ask it, `question` asked. When the owner answers, `question` with their words. "
                f"What: {q['body']} · Why: {q['context'] or '-'}]"
            )
        elif not batch[0].internal and (q := await open_item_question(self.db)):
            parts.append(
                f"[open question #Q{q['id']} from {q['owner']}: ask only if it fits naturally now, in your own words; if you "
                f"ask it, `question` asked. {question_brief(q)}]"
            )
        if not batch[0].internal:
            for q in await in_progress(self.db):
                again = ("Already brought up again once: don't again." if q.get("reminded_at") else
                         "If it got lost in the conversation and there is a natural pause, you may bring it up once more, lightly "
                         "(\"Förresten, jag undrade…\"), then `question` with reminded=true. Only once.")
                parts.append(
                    f"[finding out #Q{q['id']} for {q['owner']}: if the owner's words bear on it, follow up yourself until you have "
                    f"what it needs to settle, no more; then `question` with the answer: what you learned, in their words where "
                    f"they matter. {again} {question_brief(q)}]")
        if recall := await self._recall(batch):
            parts.append(recall)
        for m in batch:
            if m.internal:
                parts.append(f"[internal task: not from the owner. Your reply is sent to them verbatim as a message from you]\n{m.text}")
                continue
            if m.replied_to_text:
                quoted = m.replied_to_text if len(m.replied_to_text) <= 300 else m.replied_to_text[:300] + "…"
                parts.append(f'[replying to: "{quoted}"]')
            parts.append(m.text)
        return "\n".join(parts)

    async def _recall(self, batch: list[Incoming]) -> str | None:
        """Before the turn, without Claude: what the search finds in earlier conversation, posts and records for what
        the owner just wrote, so what was said comes up even when the chat doesn't think to look (19 Oct: "varifrån
        kom track 15 okt?" was answered "inte något du har sagt")."""
        text = " ".join(m.text for m in batch if not m.internal)
        if not text.strip():
            return None
        top = await self.db.fetchone("SELECT max(id) AS m FROM messages")
        try:
            hits = await search(self.db, text, limit=RECALL, exclude_messages_after=(top["m"] or 0) - RECENT)
        except Exception:
            log.exception("recall failed")
            return None
        if not hits:
            return None
        return "[minne: tidigare om det här, med vem och när]\n" + "\n".join(h.line(self.settings.timezone) for h in hits) + "\n[end]"

    async def _recent_conversation(self, limit: int = 30, before: int | None = None) -> str:
        """Bridges a new or lost session with the latest messages from the database, up to (not including) the turn's
        own messages, which follow in the prompt anyway."""
        rows = await self.db.fetchall(
            "SELECT direction, origin, text FROM messages WHERE text != '' AND id < ? ORDER BY id DESC LIMIT ?", (before or 1 << 62, limit)
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
            "SELECT m.id, m.text, m.created_at, (SELECT group_concat(ref, ' ') FROM message_refs r WHERE r.message_id = m.id) AS refs "
            "FROM messages m WHERE m.direction = 'out' AND COALESCE(m.origin, 'chat') != 'chat' AND m.in_session = 0 AND m.id > ? ORDER BY m.id",
            (seen,),
        )
        top = await self.db.fetchone("SELECT max(id) AS m FROM messages")
        await self.db.kv_set(SEEN_KEY, top["m"] or 0)
        out = []
        tz = ZoneInfo(self.settings.timezone)
        for r in rows[-PROACTIVE_LIMIT:]:
            when = datetime.fromisoformat(r["created_at"] + "+00:00").astimezone(tz)
            text = r["text"] if len(r["text"]) <= 1500 else r["text"][:1500] + "…"
            about = f" · about {r['refs']}" if r["refs"] else ""
            out.append(f"[you sent on your own {WEEKDAYS[when.weekday()]} {when:%H:%M}{about}]\n{text}\n[end]")
        return out

    async def _deliver(self, msg: Incoming) -> None:
        """The deliver skill (instance/granskning/2026-10-20-omgang-4-rosten.md, 4.1 B): the same voice and the same
        rules, with a short brief instead of the whole conversation: the latest messages, what to deliver and why, the
        profile and Manager's policy. What it sends is shown to the conversation at the owner's next turn."""
        from agent.office_tools import SERVER, office_tools

        tools = [t for t in office_tools(self.db, "assistant") if t.name in ("find", "read")]
        prompt = "\n".join([
            f"[time: {self._now()}]",
            await self._schedule_line(),
            "The conversation lately (oldest first):",
            await self._latest(DELIVER_CONTEXT),
            "",
            "Deliver now:",
            msg.text,
        ])
        result = await self.gate.run(ClaudeTask(
            kind="deliver",
            prompt=prompt,
            priority=Priority.CHAT,
            model=self.settings.models.chat,
            allowed_tools=[f"mcp__{SERVER}__find", f"mcp__{SERVER}__read"],
            mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=tools)},
            output_schema=REPLY,
            max_turns=6,
            system_prompt_append=prompt_template("deliver")
            + f"\n\n# Length\nKeep this message under about {self.settings.message_length} characters (a setting Manager tries and changes)."
            + "\n\n# Manager's policy: the situation, pacing and how you write (follow it)\n" + self.memory.read("policy.md")
            + "\n\n# The profile (what interests " + personalize("{owner}") + ", and what never to suggest)\n" + self.memory.read("profile.md"),
        ))
        if result.is_error:
            raise RuntimeError(result.error or "deliver error")
        reply = message_of(result)
        if reply in ("", SKIP):
            raise AlreadySaid()
        ids = await self._send(reply, origin="proactive", in_session=False)
        if ids and msg.about:
            await refs_mod.link(self.db, ids, set(msg.about))
        if msg.done and not msg.done.done():
            msg.done.set_result(ids)

    async def _schedule_line(self) -> str:
        if self.schedule is None:
            return ""
        try:
            return f"[schedule: {(await self.schedule.availability()).status_line()}]"
        except Exception:
            log.exception("availability failed")
            return ""

    async def _latest(self, limit: int) -> str:
        """The latest messages with who and when: what a delivery reads instead of the whole conversation."""
        rows = await self.db.fetchall("SELECT direction, origin, text, created_at FROM messages WHERE text != '' ORDER BY id DESC LIMIT ?", (limit,))
        tz = ZoneInfo(self.settings.timezone)
        lines = []
        for r in reversed(rows):
            when = datetime.fromisoformat(r["created_at"] + "+00:00").astimezone(tz)
            who = "owner" if r["direction"] == "in" else ("you (on your own)" if r["origin"] == "proactive" else "you")
            text = r["text"] if len(r["text"]) <= 700 else r["text"][:700] + "…"
            lines.append(f"{WEEKDAYS[when.weekday()]} {when:%H:%M} {who}: {text}")
        return "\n".join(lines) or "(nothing yet)"

    async def _turn(self, batch: list[Incoming]) -> None:
        if batch[0].deliver:
            await self._deliver(batch[0])
            return
        turn = TurnContext(replied_to_message_id=next((m.replied_to_message_id for m in reversed(batch) if m.replied_to_message_id), None),
                           message_row_id=next((m.message_row_id for m in reversed(batch) if m.message_row_id), None))
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
        pursuing = await Pursuits(self.db, self.settings.timezone).overview(notes=False, ideas=False)
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
                output_schema=REPLY,
                system_prompt_append=prompt_template("chat")
                + "\n\n# Manager's policy: the situation, pacing and how you write (follow it)\n"
                + self.memory.read("policy.md")
                + "\n\n# The profile, with interests and what never to suggest (applies even to things discussed earlier)\n"
                + self.memory.read("profile.md")
                + "\n\n# Goals and what " + personalize("{owner}") + " goes for (keep what they go for current with track_pursuit)\n"
                + pursuing
                + ("\n\n# Your working memory (from your last night pass)\n" + working_memory if working_memory else ""),
            )

        typing = None if internal else asyncio.create_task(self._keep_typing())
        try:
            result = await self.gate.run(task(session_id))
            if result.is_error and session_id:
                # The stored session may be gone (e.g. new machine); start a fresh one once.
                log.warning("chat resume failed (%s), starting a new session", result.error)
                result = await self.gate.run(task(None, await self._recent_conversation(before=_first_row(batch))))
        except QuotaPaused as e:
            if internal:
                raise
            if e.why.startswith("budget"):   # a guest's cap (agent/budget.py)
                await self._send("Jag har tänkt klart för i dag. Skriv igen i morgon så fortsätter vi.", origin="chat", in_session=True)
            else:
                await self._send(f"Min Claude-kvot är slut just nu. Den återställs {datetime.fromtimestamp(e.resets_at):%H:%M}.", origin="chat", in_session=True)
            return
        finally:
            if typing:
                typing.cancel()

        if result.is_error:
            if internal:
                raise RuntimeError(result.error or "chat error")
            log.warning("chat turn failed: %s", result.error)
            await self._send(failure_words(result.error), origin="chat", in_session=True)   # never the raw error (22 Oct)
            return

        if result.session_id:
            await self.db.kv_set(SESSION_KEY, result.session_id)
            await self.db.kv_set(SEED_KEY, None)
        ids: list[int] = []
        reply = message_of(result)
        if internal and reply in ("", SKIP):  # a delivery that would only repeat what was already said
            if batch[0].done and not batch[0].done.done():
                batch[0].done.set_exception(AlreadySaid())
            return
        if reply:
            ids = await self._send(reply, origin="proactive" if internal else "chat", in_session=True)
        else:
            log.warning("chat turn ended without a message; nothing sent")
        if turn.refs:  # link what this turn was about to its messages, so it can be looked up later
            await refs_mod.link(self.db, ids + [m.message_row_id for m in batch if m.message_row_id], turn.refs)
        if internal and batch[0].done and not batch[0].done.done():
            batch[0].done.set_result(ids)
        await self.memory.commit_if_changed(f"chat {self._now()}", by="assistant")

    async def _keep_typing(self) -> None:
        while True:
            try:
                await self._typing()
            except Exception:
                pass
            await asyncio.sleep(4.5)
