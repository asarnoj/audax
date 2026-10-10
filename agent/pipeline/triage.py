"""Triage: untriaged events -> value features + draft message -> outbox candidates (docs/ATTENTION.md §4.1)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import Settings
from agent.db import Database
from agent.memory import MemoryRepo
from agent.pipeline.outbox import add_candidate
from agent.prompts import prompt as prompt_template
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
                    "message": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["event_id", "keep", "relevance", "actionability", "time_sensitivity", "novelty", "confidence", "complement", "message", "reason"],
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

    async def run(self) -> int:
        """Triages pending events in batches; returns the number of candidates created."""
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
        events = "\n\n".join(f"[event_id {r['id']}] källa: {r['source']}\n{r['payload']}" for r in rows)
        prompt = prompt_template("triage").format(
            now=f"{now:%Y-%m-%d %H:%M}",
            memory=self.memory.read("direction/goals.md", "direction/now.md", "profile.md", "interests.md", "projects.md", "school.md"),
            policy=self.memory.read("policy.md", "state.md"),
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
        created = 0
        for item in result.structured.get("items", []):
            row = by_id.get(item.get("event_id"))
            if row is None:
                continue
            payload = json.loads(row["payload"])
            features = {k: item[k] for k in FEATURES if k in item}
            features["reason"] = item.get("reason", "")
            cid = await add_candidate(
                self.db,
                kind=row["kind"],
                title=payload.get("title", row["kind"]),
                body=item.get("message") or payload.get("summary", ""),
                features=features,
                url=payload.get("url"),
                expires_at=_expiry(payload, self.settings.timezone),
                event_id=row["id"],
            )
            if not item.get("keep", True):
                await self.db.execute("UPDATE candidates SET status = 'dropped', tier = 'drop' WHERE id = ?", (cid,))
                await self.db.execute(
                    "INSERT INTO decisions (candidate_id, module, tier, reason) VALUES (?, ?, 'drop', ?)",
                    (cid, MODULE, item.get("reason", "")),
                )
            else:
                created += 1

        # Mark the whole batch, including events the model skipped, so nothing loops forever.
        marks = ",".join("?" * len(rows))
        await self.db.execute(f"UPDATE events SET triaged_at = datetime('now') WHERE id IN ({marks})", tuple(by_id))
        log.info("triage: %d events -> %d candidates", len(rows), created)
        return created


def _expiry(payload: dict, tz: str) -> str | None:
    """Candidates expire at the end of their deadline/event day."""
    day = payload.get("deadline") or payload.get("date")
    if not day:
        return None
    try:
        d = datetime.fromisoformat(day[:10]).replace(hour=23, minute=59, tzinfo=ZoneInfo(tz))
    except ValueError:
        return None
    return utc_iso(d)
