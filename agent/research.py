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
from agent.pursuits import judged_against
from agent.tools import TurnContext, build_web_tools

log = logging.getLogger(__name__)


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
                    "checked": {"type": "string", "enum": ["page", "other_sites", "search_results"],
                                "description": "Where the facts come from: the page at `url` itself, copies on other sites, or only search results"},
                    "unconfirmed": {"type": ["string", "null"],
                                    "description": "Swedish: what in the summary is NOT confirmed by the page at `url` (a date, the place, who may apply), or null"},
                },
                "required": ["title", "url", "summary", "why_you", "kind", "query", "checked", "unconfirmed"],
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
RETIRE_AFTER_RUNS = 3  # without a single kept finding: shown to Researcher as worth replacing
RUNS_KEY = "research.query_runs.{id}"

KEPT = ("sent", "shelved", "pulled")


@dataclass(frozen=True)
class ResearchTask:
    id: str
    goal: str
    scope: str
    interval_days: float
    enabled: bool
    queries: tuple[str, ...] = ()   # the mission's portfolio, part of its routine (agent/routines.py)


def hold_to_the_page(finding: dict, pages_read: set[str]) -> None:
    """A finding's facts must come from the page it links to. If that page was never read in the run, the finding says
    so, whatever the run claimed (9 Oct: a Stockholm posting's facts were linked to the London posting, whose page
    couldn't be read; the caveat lay in a free-text field and was lost on the way, until the owner was told the wrong
    deadline on 23 Oct)."""
    read = {normalize_url(u) for u in pages_read}
    if normalize_url(finding["url"]) in read:
        return
    if finding.get("checked") == "page":
        finding["checked"] = "other_sites"
    note = "Sidan på länken gick inte att läsa i sökningen, så uppgifterna är inte bekräftade där."
    finding["unconfirmed"] = (finding.get("unconfirmed") or "").strip() + (" " if finding.get("unconfirmed") else "") + note


def load_research() -> list[ResearchTask]:
    from agent.sources.streams import CONFIG_ERRORS

    from agent import routines

    try:
        data = routines.missions()
        if data is None:
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
            queries=tuple(q for q in r.get("queries") or [] if isinstance(q, str) and q.strip()),
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
    """The query portfolio of one mission, kept in its routine: select (explore/exploit), and say which queries have
    run long without anything kept, for Researcher to replace (instance/granskning/2026-10-20-omgang-3-flodet.md, 5:
    the queries are part of the routine). How often each ran is kept per mission; what each found comes from the
    findings themselves."""

    def __init__(self, db: Database, gate: ClaudeGate, memory: MemoryRepo, settings: Settings):
        self.db = db
        self.gate = gate
        self.memory = memory
        self.settings = settings

    async def stats(self, mission: str, queries: tuple[str, ...] | list[str] | None = None) -> list[dict]:
        """Every query with runs, findings and kept findings (sent, shelved, pulled from the shelf, or planned)."""
        if queries is None:
            queries = next((t.queries for t in load_research() if t.id == mission), ())
        runs = await self.db.kv_get(RUNS_KEY.format(id=mission), {}) or {}
        out = []
        for q in queries:
            row = await self.db.fetchone(
                "SELECT count(*) AS found, sum(c.status IN ('sent', 'shelved', 'pulled', 'scheduled')) AS kept FROM events e "
                "LEFT JOIN candidates c ON c.event_id = e.id WHERE e.source = ? AND json_extract(e.payload, '$.query') = ?",
                (f"research:{mission}", q))
            out.append({"query": q, "runs": int(runs.get(q, 0)), "found": row["found"] or 0, "kept": row["kept"] or 0})
        return out

    async def select(self, task: ResearchTask, batch: int = BATCH) -> list[str]:
        qs = await self.stats(task.id, task.queries)
        untested = [q for q in qs if q["runs"] == 0]
        tested = sorted((q for q in qs if q["runs"] > 0), key=lambda q: (q["kept"] + 1) / (q["runs"] + 2), reverse=True)
        explore = untested[: max(EXPLORE, batch // 3)]
        exploit = tested[: batch - len(explore)]
        chosen = explore + exploit
        if len(chosen) < batch:  # not enough history yet: fill with more untested
            chosen += [q for q in untested if q not in chosen][: batch - len(chosen)]
        return [q["query"] for q in chosen]

    async def record_run(self, mission: str, queries: list[str]) -> None:
        runs = await self.db.kv_get(RUNS_KEY.format(id=mission), {}) or {}
        for q in queries:
            runs[q] = int(runs.get(q, 0)) + 1
        await self.db.kv_set(RUNS_KEY.format(id=mission), runs)

    async def stale(self, mission: str) -> list[str]:
        """Queries that have run a few times without a single kept finding: worth replacing."""
        return [q["query"] for q in await self.stats(mission) if q["runs"] >= RETIRE_AFTER_RUNS and q["kept"] == 0]


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
        turn = TurnContext()
        web_servers, web_names = build_web_tools(self.web, turn) if self.web else ({}, [])
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
            memory=await judged_against(self.db, self.memory, self.settings.timezone),
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
            hold_to_the_page(f, turn.pages_read)
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
