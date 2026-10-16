"""Scheduled research: Claude searches the web for opportunities and returns structured findings as events."""

from __future__ import annotations

from agent import trail

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yaml

from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import Settings, instance_config
from agent.db import Database
from agent.memory import MemoryRepo
from agent import search_map
from agent.prompts import prompt as prompt_template
from agent.tools import build_web_tools

log = logging.getLogger(__name__)

MEMORY_FILES = ("direction/goals.md", "direction/now.md", "profile.md", "interests.md", "projects.md", "school.md")

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "summary": {"type": "string", "description": "Swedish. The facts: what, where, when, requirements"},
                    "why_you": {"type": "string", "description": "Swedish. Why it fits the owner specifically"},
                    "kind": {"type": "string", "enum": ["opportunity", "event", "news", "tool", "social"]},
                    "deadline": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                    "date": {"type": ["string", "null"], "description": "YYYY-MM-DD for events"},
                    "location": {"type": ["string", "null"]},
                    "organization": {"type": ["string", "null"], "description": "Who offers it (company, university, organisation)"},
                    "query": {"type": "string", "description": "The query (exactly as given) that led to the finding"},
                },
                "required": ["title", "url", "summary", "why_you", "kind", "query"],
            },
        },
        "leads": {
            "type": "array",
            "description": "Places and organisations you came across that Audax may not watch yet: companies with relevant "
            "openings or programmes, aggregators, lists, communities, event calendars. Not findings in themselves.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "url": {"type": ["string", "null"]},
                    "kind": {"type": "string", "enum": ["company", "aggregator", "list", "programme", "community", "event_calendar", "other"]},
                    "why": {"type": "string", "description": "Swedish, one line"},
                },
                "required": ["name", "kind", "why"],
            },
        },
        "skipped": {
            "type": "array",
            "description": "What you found but left out (already known, expired, not interested, doesn't fit), with a short reason in Swedish",
            "items": {"type": "object", "properties": {"title": {"type": "string"}, "reason": {"type": "string"}}, "required": ["title", "reason"]},
        },
    },
    "required": ["findings", "leads", "skipped"],
}


# Portfolio mechanics (docs/SENSES.md, layer 3). These are search mechanics, not attention policy.
BATCH = 6  # queries per run
EXPLORE = 2  # of which untested
DEEP_BATCH = 20  # weekly deep sweep: wide, like a deep-research run
DEEP_SPACING = timedelta(hours=3)  # never two deep sweeps close together (quota)
DEEP_NOTE = (
    "\nThis is the weekly deep sweep: run every query, follow leads, read 20-40 pages if useful, and look into adjacent "
    "angles and new kinds of sources. Wider coverage, the same bar for quality.\n"
)
RETIRE_AFTER_RUNS = 3  # without a single kept finding

KEPT = ("sent", "shelved", "pulled")


@dataclass(frozen=True)
class ResearchTask:
    id: str
    goal: str
    scope: str
    interval_days: float
    enabled: bool


def load_research() -> list[ResearchTask]:
    from agent.sources.streams import CONFIG_ERRORS

    try:
        data = yaml.safe_load(instance_config("research.yaml").read_text()) or []
        CONFIG_ERRORS.pop("research.yaml", None)
    except Exception as e:
        CONFIG_ERRORS["research.yaml"] = f"{type(e).__name__}: {str(e)[:150]}"
        log.error("research.yaml unreadable, no missions until fixed: %s", e)
        return []
    return [
        ResearchTask(
            id=r["id"],
            goal=" ".join(r["goal"].split()),
            scope=r.get("scope", ""),
            interval_days=float(r.get("interval_days", 7)),
            enabled=r.get("enabled", True),
        )
        for r in data
    ]


def normalize_url(url: str) -> str:
    """Dedupe key: no fragment, no tracking query, no trailing slash."""
    url = url.strip().split("#")[0]
    if "utm_" in url:
        url = url.split("?")[0]
    return url.rstrip("/").lower()


class Portfolio:
    """The query portfolio of one mission: select (explore/exploit) and retire. New queries come from Researcher's
    planning (agent/research_plan.py), aimed at the gaps in the mission's map."""

    def __init__(self, db: Database, gate: ClaudeGate, memory: MemoryRepo, settings: Settings):
        self.db = db
        self.gate = gate
        self.memory = memory
        self.settings = settings

    async def stats(self, mission: str) -> list[dict]:
        """Every query with runs, findings and kept findings (sent, shelved or pulled from the shelf)."""
        rows = await self.db.fetchall(
            "SELECT q.id, q.query, q.status, q.runs, "
            "(SELECT count(*) FROM events e WHERE e.source = 'research:' || q.mission "
            "   AND json_extract(e.payload, '$.query') = q.query) AS found, "
            "(SELECT count(*) FROM events e JOIN candidates c ON c.event_id = e.id WHERE e.source = 'research:' || q.mission "
            "   AND json_extract(e.payload, '$.query') = q.query AND c.status IN ('sent', 'shelved', 'pulled', 'scheduled')) AS kept "
            "FROM scout_queries q WHERE q.mission = ? ORDER BY q.id",
            (mission,),
        )
        return [dict(r) for r in rows]

    async def select(self, task: ResearchTask, batch: int = BATCH) -> list[str]:
        await self._retire(task.id)
        active = [q for q in await self.stats(task.id) if q["status"] == "active"]  # a thin portfolio makes planning due
        untested = [q for q in active if q["runs"] == 0]
        tested = sorted((q for q in active if q["runs"] > 0), key=lambda q: (q["kept"] + 1) / (q["runs"] + 2), reverse=True)
        explore = untested[: max(EXPLORE, batch // 3)]
        exploit = tested[: batch - len(explore)]
        chosen = explore + exploit
        if len(chosen) < batch:  # not enough history yet: fill with more untested
            chosen += [q for q in untested if q not in chosen][: batch - len(chosen)]
        return [q["query"] for q in chosen]

    async def record_run(self, mission: str, queries: list[str]) -> None:
        for q in queries:
            await self.db.execute(
                "UPDATE scout_queries SET runs = runs + 1, last_run = datetime('now') WHERE mission = ? AND query = ?", (mission, q)
            )

    async def _retire(self, mission: str) -> None:
        for q in await self.stats(mission):
            if q["status"] == "active" and q["runs"] >= RETIRE_AFTER_RUNS and q["kept"] == 0:
                await self.db.execute("UPDATE scout_queries SET status = 'retired' WHERE id = ?", (q["id"],))


class Researcher:
    def __init__(self, settings: Settings, db: Database, gate: ClaudeGate, memory: MemoryRepo):
        self.settings = settings
        self.db = db
        self.gate = gate
        self.memory = memory
        self.tz = ZoneInfo(settings.timezone)
        self.portfolio = Portfolio(db, gate, memory, settings)
        self.web = None  # WebReader, set by main

    async def due(self) -> list[ResearchTask]:
        now = datetime.now(self.tz)
        out = []
        for t in load_research():
            if not t.enabled:
                continue
            last = await self.db.kv_get(f"research.last_run.{t.id}")
            if last is None or now - datetime.fromisoformat(last) >= timedelta(days=t.interval_days):
                out.append(t)
        return out

    async def due_deep(self) -> ResearchTask | None:
        """A mission Researcher's planning asked to sweep widely, if none ran recently (Researcher decides when)."""
        now = datetime.now(self.tz)
        last_any = await self.db.kv_get("research.last_deep_any")
        if last_any and now - datetime.fromisoformat(last_any) < DEEP_SPACING:
            return None
        due = []
        for t in load_research():
            requested = await self.db.kv_get(f"research.deep_requested.{t.id}")
            if t.enabled and requested:
                due.append((requested, t))
        return min(due, key=lambda x: x[0])[1] if due else None

    async def run(self, task: ResearchTask, deep: bool = False) -> int:
        """Runs one research task; returns the number of new findings stored as events."""
        source = f"research:{task.id}"
        seen = await self.db.fetchall(
            "SELECT payload FROM events WHERE source = ? AND created_at >= datetime('now', '-90 days') ORDER BY id DESC LIMIT 60",
            (source,),
        )
        seen_lines = [f"- {json.loads(r['payload'])['title']} ({json.loads(r['payload'])['url']})" for r in seen]
        try:
            queries = await self.portfolio.select(task, DEEP_BATCH if deep else BATCH)
        except QuotaPaused:
            log.info("research %s postponed: quota paused", task.id)
            return 0
        web_servers, web_names = build_web_tools(self.web) if self.web else ({}, [])
        if deep:
            now = datetime.now(self.tz).isoformat()
            await self.db.kv_set(f"research.last_deep.{task.id}", now)
            await self.db.kv_set("research.last_deep_any", now)
            await self.db.kv_set(f"research.deep_requested.{task.id}", None)
        prompt = prompt_template("research").format(
            depth=DEEP_NOTE if deep else "",
            queries="\n".join(f"- {q}" for q in queries) or "(none; search freely)",
            id=task.id,
            goal=task.goal,
            scope=task.scope,
            today=datetime.now(self.tz).date().isoformat(),
            memory=self.memory.read(*MEMORY_FILES),
            seen="\n".join(seen_lines) or "(nothing yet)",
            gaps=await search_map.gaps(self.memory.path, task.id) or "(no map yet: explore freely)",
        )
        try:
            result = await self.gate.run(
                ClaudeTask(
                    kind="research_deep" if deep else "research",
                    prompt=prompt,
                    priority=Priority.RESEARCH,
                    model=self.settings.models.research,
                    allowed_tools=["WebSearch", *web_names],
                    mcp_servers=web_servers,
                    output_schema=FINDINGS_SCHEMA,
                    max_turns=120 if deep else 40,
                )
            )
        except QuotaPaused:
            log.info("research %s postponed: quota paused", task.id)
            return 0

        # Count the attempt even on failure so a broken task doesn't retry every 30 minutes.
        await self.db.kv_set(f"research.last_run.{task.id}", datetime.now(self.tz).isoformat())
        await self.portfolio.record_run(task.id, queries)
        if result.is_error or not isinstance(result.structured, dict):
            error = result.error or "inget strukturerat svar"
            await self.db.execute("INSERT INTO research_runs (research_id, error) VALUES (?, ?)", (task.id, error))
            log.warning("research %s failed: %s", task.id, error)
            return 0

        new = 0
        findings = result.structured.get("findings", [])
        for f in findings:
            if not f.get("url") or not f.get("title"):
                continue
            if f.get("query") not in queries:  # keep yield attribution honest
                f["query"] = next((q for q in queries if q.lower() in str(f.get("query", "")).lower()), f.get("query"))
            new += bool(await self.db.insert("events", ignore=True, source=source, kind=f.get("kind", "opportunity"),
                                             dedupe_key=normalize_url(f["url"]), payload=json.dumps(f, ensure_ascii=False)))
        if new:
            await trail.emit("print", "director", n=new)   # a search's findings come out of the printer too
        new_leads = await search_map.record_leads(self.db, task.id, result.structured.get("leads") or [])
        skipped = result.structured.get("skipped") or []
        await self.db.execute(
            "INSERT INTO research_runs (research_id, findings, returned, skipped) VALUES (?, ?, ?, ?)",
            (task.id, new, len(findings), json.dumps(skipped, ensure_ascii=False)),
        )
        searches = sum(1 for t in result.trace if t["tool"] == "WebSearch")
        log.info(
            "research %s: %d searches, %d findings returned, %d new, %d skipped, %d new leads (%.0fs)",
            task.id, searches, len(findings), new, len(skipped), new_leads, (result.duration_ms or 0) / 1000,
        )
        return new
