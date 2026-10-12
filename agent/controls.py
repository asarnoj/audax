"""User controls, shared by chat tools (natural language) and the fallback slash commands."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from agent.pipeline.outbox import AlreadySaid
from agent.db import Database
from agent.pipeline.dispatcher import FOCUS_KEY, SILENCE_KEY, Dispatcher
from agent.pipeline.outbox import render
from agent.research import load_research

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[object]]


class Controls:
    def __init__(self, db: Database, dispatcher: Dispatcher, research: Callable[[str], Awaitable[int]], notify: Notify):
        self.db = db
        self.dispatcher = dispatcher
        self._research = research
        self._notify = notify
        self.speak = None  # set by main: the chat session writes what the owner reads
        self._background: set[asyncio.Task] = set()

    async def _tell(self, instruction: str, fallback: str) -> None:
        """Through the one voice when it is there; a plain line only if the chat can't write."""
        if self.speak is not None:
            try:
                await self.speak(instruction)
                return
            except AlreadySaid:
                return
            except Exception as e:
                log.warning("chat couldn't tell the owner (%s); sending a plain line", e)
        await self._notify(fallback)

    async def _signal(self, value: str) -> None:
        await self.db.execute("INSERT INTO feedback (signal, value) VALUES ('command', ?)", (value,))

    # --- pausing --------------------------------------------------------------

    async def pause(self, mode: str, minutes: int) -> str:
        """mode quiet: nothing proactive; focus: only things allowed to interrupt. minutes=0 ends it."""
        key, label = (SILENCE_KEY, "Tyst läge") if mode == "quiet" else (FOCUS_KEY, "Fokusläge")
        if minutes <= 0:
            await self.dispatcher.silence(key, None)
            await self._signal(f"{mode}:av")
            return f"{label} avslutat."
        until = self.dispatcher.schedule.now() + timedelta(minutes=minutes)
        await self.dispatcher.silence(key, until)
        await self._signal(f"{mode}:{minutes}m")
        return f"{label} till {until:%H:%M}."

    # --- shelf ----------------------------------------------------------------

    async def shelf(self, about: str | None = None) -> tuple[str, list]:
        """What is saved for later, for the chat to pick from. Nothing changes until it marks what it told the owner."""
        rows = await self.dispatcher.outbox.peek_shelf(about=about)
        if not rows:
            return "[internal] Nothing saved for later.", []
        return "[internal] Saved for later (id · kind · what):\n\n" + "\n\n".join(f"[{r['id']}] {r['kind']} · {render(r)}" for r in rows), rows

    # --- why ------------------------------------------------------------------

    async def last_proactive_message_id(self) -> int | None:
        row = await self.db.fetchone(
            "SELECT m.id FROM messages m WHERE m.direction = 'out' AND m.origin = 'proactive' "
            "AND (m.candidate_id IS NOT NULL OR m.id IN (SELECT message_id FROM message_candidates)) ORDER BY m.id DESC LIMIT 1"
        )
        return row["id"] if row else None

    async def why(self, message_row_id: int | None) -> str:
        if message_row_id is None:
            return "Jag har inte skickat något på eget initiativ än."
        rows = await self.db.fetchall(
            "SELECT c.id, c.kind, c.title, c.features, d.module, d.tier, d.reason, d.context "
            "FROM candidates c JOIN decisions d ON d.candidate_id = c.id "
            "WHERE c.id IN (SELECT candidate_id FROM message_candidates WHERE message_id = ? "
            "               UNION SELECT candidate_id FROM messages WHERE id = ?) "
            "ORDER BY c.id, d.id",
            (message_row_id, message_row_id),
        )
        if not rows:
            return "Det där var ett chattsvar, inget jag valde att skicka på eget initiativ."
        await self._signal("varfor")
        parts = []
        for r in rows:
            f = json.loads(r["features"])
            ctx = json.loads(r["context"] or "{}")
            feats = ", ".join(f"{k}={v}" for k, v in f.items() if k != "reason")
            parts.append(
                f"{r['title']} ({r['kind']})\n"
                f"Beslut: {r['tier']} av {r['module']}\n"
                f"Varför: {r['reason']}\n"
                + (f"Bedömning: {feats}\n" if feats else "")
                + (f"Triage: {f['reason']}\n" if f.get("reason") else "")
                + (f"Läge då: {ctx['availability']}" if ctx.get("availability") else "")
            )
        return "\n\n".join(parts)

    # --- sources and research ---------------------------------------------------

    async def sources(self) -> str:
        rows = await self.db.fetchall(
            "SELECT e.source, count(*) AS n, "
            "sum(c.status = 'sent') AS sent, sum(c.status IN ('shelved', 'pulled')) AS shelf, sum(c.status = 'dropped') AS dropped "
            "FROM events e LEFT JOIN candidates c ON c.event_id = e.id "
            "WHERE e.created_at >= datetime('now', '-7 days') AND COALESCE(e.triaged_at, '') != 'baseline' "
            "GROUP BY e.source ORDER BY n DESC"
        )
        research = ", ".join(f"{t.id} (var {t.interval_days:g}:e dag)" for t in load_research() if t.enabled)
        from agent.sources.streams import load_stream_configs

        kinds = {"rss": "flöde", "page": "sida", "topic": "nyhetsbevakning (nyhetssök + Hacker News)", "jobtech": "Platsbanken",
                 "ats": "företagens jobbannonser", "devpost": "Devpost-hackathons", "hn_hiring": "HN Who is hiring", "websocket": "liveflöde"}
        def pace(m):
            return f"var {m / 60:g}:e timme" if m >= 60 else f"var {m:g}:e minut"
        senses = [
            f"- {c.id}: {kinds.get(c.type, c.type)}, {pace(c.poll_minutes)}"
            + (f" ({c.params.get('query')})" if c.params.get("query") else (f" ({c.url})" if c.url else ""))
            for c in load_stream_configs() if c.enabled
        ]
        lines = ["Det jag bevakar och hur ofta:", *senses, "", "Utfall senaste 7 dagarna (in → skickat / hylla / bortsorterat):"]
        lines += [f"- {r['source']}: {r['n']} → {r['sent'] or 0} / {r['shelf'] or 0} / {r['dropped'] or 0}" for r in rows] or ["- inga händelser än"]
        lines.append(f"Undersökningar: {research or 'inga'}")
        return "\n".join(lines)

    async def recent_items(self, source: str, n: int = 10) -> str:
        """The latest items from one sense (matched loosely on its id), with what happened to each and why."""
        rows = await self.db.fetchall(
            "SELECT e.source, e.created_at, json_extract(e.payload, '$.title') AS title, c.status, "
            "(SELECT d.reason FROM decisions d WHERE d.candidate_id = c.id ORDER BY d.id DESC LIMIT 1) AS reason, "
            "json_extract(c.features, '$.reason') AS triage "
            "FROM events e LEFT JOIN candidates c ON c.event_id = e.id "
            "WHERE e.source LIKE ? AND COALESCE(e.triaged_at, '') != 'baseline' ORDER BY e.id DESC LIMIT ?",
            (f"%{source}%", n),
        )
        if not rows:
            return f"[internal] Nothing has come in from {source} yet (beyond what existed when watching started)."
        status = {"sent": "sent to the owner", "shelved": "saved for later", "pulled": "told on request", "dropped": "left out",
                  "pending": "waiting to be sent", "sending": "being sent", "expired": "expired", None: "not judged yet"}
        return "[internal] Latest items and what happened to each:\n" + "\n".join(
            f"- {r['created_at'][:10]} {r['title'] or '?'} → {status.get(r['status'], r['status'])}"
            + (f": {(r['reason'] or r['triage'] or '')[:140]}" if (r["reason"] or r["triage"]) else "")
            for r in rows
        )

    def research_ids(self) -> list[str]:
        return [t.id for t in load_research()]

    async def start_research(self, research_id: str) -> str:
        if research_id not in self.research_ids():
            return f"Finns ingen undersökning som heter {research_id}. Finns: {', '.join(self.research_ids())}."
        await self._signal(f"sok:{research_id}")

        async def go() -> None:
            try:
                n = await self._research(research_id)
                found = await self.recent_items(f"research:{research_id}", n) if n else ""
                outcome = f"finished with {n} new finding(s):\n{found}" if n else "finished without anything new"
            except Exception:
                log.exception("research %s failed", research_id)
                outcome = "failed"
            await self._tell(
                f"The search you started for the owner ({research_id}) {outcome}\nTell them what it found, concretely and "
                "briefly, in their terms (what it is, and whether you sent it, saved it for later or left it out). If you "
                "already said all of it, reply SKIP.",
                fallback="Sökningen du bad om är klar." if outcome != "failed" else "Sökningen du bad om gick inte att köra just nu.",
            )

        task = asyncio.create_task(go())
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return f"[internal] Started {research_id}; it takes a few minutes, and you will be asked to tell the owner the result."
