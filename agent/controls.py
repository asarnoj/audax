"""User controls, shared by chat tools (natural language) and the fallback slash commands."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

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
        self._background: set[asyncio.Task] = set()

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

    async def shelf(self) -> tuple[str, list]:
        rows = await self.dispatcher.outbox.pull_shelf()
        if not rows:
            return "Hyllan är tom.", []
        return "Från hyllan:\n\n" + "\n\n".join(render(r) for r in rows), rows

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

    def research_ids(self) -> list[str]:
        return [t.id for t in load_research()]

    async def start_research(self, research_id: str) -> str:
        if research_id not in self.research_ids():
            return f"Finns ingen undersökning som heter {research_id}. Finns: {', '.join(self.research_ids())}."
        await self._signal(f"sok:{research_id}")

        async def go() -> None:
            try:
                n = await self._research(research_id)
                await self._notify(f"{research_id}: klar, {n} nya fynd." + (" Det som inte skickades ligger på hyllan." if n else ""))
            except Exception:
                log.exception("research %s failed", research_id)
                await self._notify(f"{research_id}: något gick fel.")

        task = asyncio.create_task(go())
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return f"Startade {research_id}. Det tar någon minut, jag säger till när den är klar."
