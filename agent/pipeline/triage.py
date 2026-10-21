"""Triage: untriaged events -> value features + a brief for the chat -> outbox candidates (docs/archive/ATTENTION.md §4.1).

One run at a time: several paths start triage (the senses, search missions), and two concurrent runs once
judged the same events twice (12 Oct). The lock serialises them; the unique index on candidates.event_id
is the backstop."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import Settings
from agent.db import Database
from agent.memory import MemoryRepo
from agent import trail
from agent.pipeline.outbox import add_candidate
from agent.prompts import prompt as prompt_template
from agent.pursuits import judged_against
from agent.rules import screen
from agent.schedule import utc_iso

log = logging.getLogger(__name__)

MODULE = "triage@1"
BATCH = 15
LEVEL = {"type": "string", "enum": ["low", "medium", "high"]}

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer"},
                    "keep": {"type": "boolean"},
                    "relevance": LEVEL,
                    "actionability": LEVEL,
                    "time_sensitivity": LEVEL,
                    "novelty": LEVEL,
                    "confidence": LEVEL,
                    "complement": LEVEL,
                    "brief": {"type": "string", "description": "Swedish, 1-3 sentences: what it is, why it fits, key facts"},
                    "reason": {"type": "string", "description": "Swedish, one sentence on the judgement"},
                },
                "required": ["event_id", "keep", "relevance", "actionability", "time_sensitivity", "novelty", "confidence", "complement", "brief", "reason"],
            },
        }
    },
    "required": ["items"],
}
FEATURES = ("relevance", "actionability", "time_sensitivity", "novelty", "confidence", "complement")


class Triage:
    def __init__(self, settings: Settings, db: Database, gate: ClaudeGate, memory: MemoryRepo):
        self.settings = settings
        self.db = db
        self.gate = gate
        self.memory = memory
        self._lock = asyncio.Lock()

    async def run(self) -> int:
        """Triages pending events in batches; returns the number of candidates created."""
        async with self._lock:
            return await self._run()

    async def _run(self) -> int:
        created = 0
        while rows := await self.db.fetchall(
            "SELECT * FROM events WHERE triaged_at IS NULL ORDER BY id LIMIT ?", (BATCH,)
        ):
            n = await self._batch(rows)
            if n is None:
                break
            created += n
        return created

    async def _batch(self, rows) -> int | None:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        # Code before Claude (agent/rules.py): duplicates, what has passed, and the roles' rules.
        decided = await screen(self.db, rows, self.settings.timezone)
        for row in rows:
            if row["id"] in decided:
                payload = json.loads(row["payload"])
                try:
                    cid = await add_candidate(self.db, kind=row["kind"], title=payload.get("title", row["kind"]), body=payload.get("summary", ""),
                                              features={"reason": decided[row["id"]]}, url=payload.get("url"), event_id=row["id"])
                except sqlite3.IntegrityError:
                    continue
                await self.db.execute("UPDATE candidates SET status = 'dropped', tier = 'drop' WHERE id = ?", (cid,))
                await self.db.insert("decisions", candidate_id=cid, module="rules", tier="drop", reason=decided[row["id"]])
        if decided:
            marks = ",".join("?" * len(decided))
            await self.db.execute(f"UPDATE events SET triaged_at = datetime('now') WHERE id IN ({marks})", tuple(decided))
            await trail.emit("drop", "director", n=len(decided))
            log.info("triage: %d event(s) decided by code: %s", len(decided), "; ".join(sorted(set(decided.values())))[:300])
        rows = [r for r in rows if r["id"] not in decided]
        if not rows:
            return 0
        events = "\n\n".join(f"[event_id {r['id']}] source: {r['source']}\n{r['payload']}" for r in rows)
        prompt = prompt_template("triage").format(
            now=f"{now:%Y-%m-%d %H:%M}",
            memory=await judged_against(self.db, self.memory, self.settings.timezone),
            policy=self.memory.read("policy.md"),
            recent=await self._recent(),
            events=events,
        )
        try:
            result = await self.gate.run(
                ClaudeTask(
                    kind="triage",
                    prompt=prompt,
                    priority=Priority.RESEARCH,
                    model=self.settings.models.triage,
                    output_schema=SCHEMA,
                    max_turns=3,
                )
            )
        except QuotaPaused:
            log.info("triage postponed: quota paused")
            return None
        if result.is_error or not isinstance(result.structured, dict):
            log.warning("triage failed: %s", result.error)
            return None

        by_id = {r["id"]: r for r in rows}
        created, dropped = 0, 0
        for item in result.structured.get("items", []):
            row = by_id.get(item.get("event_id"))
            if row is None:
                continue
            payload = json.loads(row["payload"])
            features = {k: item[k] for k in FEATURES if k in item}
            features["reason"] = item.get("reason", "")
            try:
                cid = await add_candidate(
                    self.db,
                    kind=row["kind"],
                    title=payload.get("title", row["kind"]),
                    body=item.get("brief") or payload.get("summary", ""),
                    features=features,
                    url=payload.get("url"),
                    expires_at=_expiry(payload, self.settings.timezone, row["kind"]),
                    event_id=row["id"],
                )
            except sqlite3.IntegrityError:  # this event already has a candidate
                log.warning("triage: event %s already has a candidate, skipped", row["id"])
                continue
            if not item.get("keep", True):
                await self.db.execute("UPDATE candidates SET status = 'dropped', tier = 'drop' WHERE id = ?", (cid,))
                await self.db.insert("decisions", candidate_id=cid, module=MODULE, tier="drop", reason=item.get("reason", ""))
                dropped += 1
            else:
                created += 1
                await trail.emit("pass", "director", ref=f"#F{cid}")

        # Mark the whole batch, including events the model skipped, so nothing loops forever.
        marks = ",".join("?" * len(rows))
        await self.db.execute(f"UPDATE events SET triaged_at = datetime('now') WHERE id IN ({marks})", tuple(by_id))
        if dropped:
            await trail.emit("drop", "director", n=dropped)
        log.info("triage: %d events -> %d candidates", len(rows), created)
        return created

    async def _recent(self) -> str:
        """What is already sent, waiting or shelved, so the same news from another source is seen as a duplicate."""
        rows = await self.db.fetchall(
            "SELECT title, url, status, updated_at FROM candidates WHERE status IN ('pending', 'sending', 'sent', 'shelved', 'pulled') "
            "AND updated_at >= datetime('now', '-14 days') ORDER BY updated_at DESC LIMIT 40"
        )
        return "\n".join(f"- [{r['status']} {r['updated_at'][:10]}] {r['title'][:100]} {r['url'] or ''}".rstrip() for r in rows) or "(nothing)"


def _expiry(payload: dict, tz: str, kind: str = "event") -> str | None:
    """Candidates expire at the end of their deadline day, or for an event the day it happens. For anything else
    `date` is just when it was published, not an end (stories expired the moment they arrived, 12 Oct)."""
    day = payload.get("deadline") or (payload.get("date") if kind == "event" else None)
    if not day:
        return None
    try:
        d = datetime.fromisoformat(day[:10]).replace(hour=23, minute=59, tzinfo=ZoneInfo(tz))
    except ValueError:
        return None
    return utc_iso(d)
