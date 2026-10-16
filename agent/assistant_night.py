"""The Assistant's slow task: the night pass (docs/MODEL.md). The one who owns the memory of the owner tidies it.

Once a night, as the first step of the night sweep, the Assistant reads the day's conversation and:
- tidies the profile (merges duplicates, removes what is no longer true, keeps the owner's own words),
- rewrites its working memory: ongoing threads of conversation, what the owner cares about right now, open promises,
- writes the conversation summary a new chat session starts from,
- books promises it made during the day that aren't booked yet (agent/agenda.py).

A chat session that has grown big resends its whole history on every reply, so after this pass the session starts
over from the summary (mechanical hygiene, not UX policy).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent import trail
from agent.agenda import Agenda
from agent.chat import ASSISTANT_FILES
from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import Settings
from agent.db import Database
from agent.memory import MemoryRepo
from agent.prompts import prompt as prompt_template
from agent.pursuits import Pursuits

log = logging.getLogger(__name__)

LAST_KEY = "assistant.last_night"
WORKING_MEMORY = "arbetsminne/assistant.md"
CHAT_SEED_KEY = "chat.seed_summary"
CHAT_CONTEXT_LIMIT = 60_000  # tokens; above this the session starts over from the summary

SCHEMA = {
    "type": "object",
    "properties": {
        "working_memory_md": {"type": "string", "description": "Swedish. Your whole new working memory"},
        "conversation_summary": {
            "type": "string",
            "description": "Swedish. What a new session needs: ongoing threads, promises, what is current, how the owner wants it",
        },
        "promises": {
            "type": "array",
            "description": "Promises you made today with a time that aren't booked yet",
            "items": {
                "type": "object",
                "properties": {
                    "when": {"type": "string", "description": "Local time, YYYY-MM-DD HH:MM"},
                    "what": {"type": "string", "description": "Swedish: what you promised"},
                    "about": {"type": ["string", "null"], "description": "An id it concerns, e.g. #S3"},
                    "latest": {"type": ["string", "null"]},
                },
                "required": ["when", "what"],
            },
        },
        "summary": {"type": "string", "description": "Swedish. What you tidied and why, briefly: the run's own account"},
    },
    "required": ["working_memory_md", "conversation_summary", "promises", "summary"],
}


class AssistantNight:
    def __init__(self, settings: Settings, db: Database, gate: ClaudeGate, memory: MemoryRepo, agenda: Agenda | None):
        self.settings = settings
        self.db = db
        self.gate = gate
        self.memory = memory
        self.agenda = agenda
        self.tz = ZoneInfo(settings.timezone)
        self._lock = asyncio.Lock()

    async def run(self) -> dict | None:
        async with self._lock:
            return await self._run()

    async def _run(self) -> dict | None:
        now = datetime.now(self.tz)
        last = await self.db.kv_get(LAST_KEY)
        since = datetime.fromisoformat(last) if last else now - timedelta(days=1)
        since_sql = since.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
        context = {
            "now": f"{now:%Y-%m-%d %H:%M}",
            "working_memory": self.memory.read(WORKING_MEMORY) or "(empty: this is your first night pass)",
            "conversation": await self._conversation(since_sql),
            "memory": self.memory.read(*[f for f in ASSISTANT_FILES if not f.startswith("arbetsminne")]),
            "planned": await self.agenda.upcoming(14) if self.agenda else "(no schedule)",
            "pursuits": await Pursuits(self.db, self.settings.timezone).overview(notes=False, ideas=False),
        }
        writes = [f"{verb}(./memory/{f})" for f in ASSISTANT_FILES if not f.startswith("arbetsminne") for verb in ("Edit", "Write")]
        try:
            result = await self.gate.run(
                ClaudeTask(
                    kind="assistant_night",
                    prompt=prompt_template("assistant_night").format(**context),
                    priority=Priority.MANAGER,
                    model=self.settings.models.manager,
                    allowed_tools=["Read(./memory/**)", *writes],
                    output_schema=SCHEMA,
                    max_turns=20,
                )
            )
        except QuotaPaused:
            log.info("assistant night pass postponed: quota paused")
            return None
        if result.is_error or not isinstance(result.structured, dict):
            log.warning("assistant night pass failed: %s", result.error)
            return None
        await self.apply(result.structured, now)
        await self.db.kv_set(LAST_KEY, now.isoformat())
        return result.structured

    async def apply(self, out: dict, now: datetime) -> list[str]:
        changes: list[str] = []
        if wm := (out.get("working_memory_md") or "").strip():
            path = self.memory.path / WORKING_MEMORY
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(wm + "\n")
        if self.agenda is not None:
            for p in out.get("promises") or []:
                at = self.agenda.parse(p.get("when") or "")
                if at is None:
                    continue
                latest = self.agenda.parse(p["latest"]) if p.get("latest") else None
                bid, conflicts = await self.agenda.book("promise", p["what"][:80], p["what"], at, "assistant", latest=latest, ref=p.get("about"))
                changes.append(f"löfte bokat #B{bid}" if bid else f"löfte kunde inte bokas ({'; '.join(conflicts)}): {p['what'][:60]}")
        if summary := (out.get("conversation_summary") or "").strip():
            await self.db.kv_set(CHAT_SEED_KEY, summary)
            if await self._session_too_big():
                await self.db.kv_set("chat.session_id", None)  # the next message starts a fresh session from the summary
                changes.append("chattsessionen startades om med sammanfattning")
        await trail.account("\n".join([(out.get("summary") or "").strip(), *(f"- {c}" for c in changes)]))
        await self.memory.commit_if_changed(f"assistant natt {now:%Y-%m-%d %H:%M}", by="assistant")
        log.info("assistant night pass: %s", "; ".join(changes) or "inga ändringar")
        return changes

    async def _session_too_big(self) -> bool:
        last = await self.db.fetchone("SELECT context_tokens FROM claude_runs WHERE kind = 'chat' ORDER BY id DESC LIMIT 1")
        return bool(last and last["context_tokens"] and last["context_tokens"] > CHAT_CONTEXT_LIMIT)

    async def _conversation(self, since: str) -> str:
        rows = await self.db.fetchall(
            "SELECT m.id, m.direction, m.origin, m.text, m.created_at, (SELECT group_concat(ref, ' ') FROM message_refs r WHERE r.message_id = m.id) AS refs "
            "FROM messages m WHERE m.created_at >= ? ORDER BY m.id", (since,),
        )
        lines = []
        for r in rows[-150:]:
            who = "owner" if r["direction"] == "in" else ("you (proactive)" if r["origin"] == "proactive" else "you")
            d = datetime.fromisoformat(r["created_at"] + "+00:00").astimezone(self.tz)
            text = r["text"] if len(r["text"]) <= 1200 else r["text"][:1200] + "…"
            lines.append(f"{d:%d/%m %H:%M} {who}: {text}" + (f" [{r['refs']}]" if r["refs"] else ""))
        return "\n".join(lines) or "No conversation since the last pass."
