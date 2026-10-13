"""Researcher's slow task: planning the search (docs/MODEL.md). How do we find what exists, and what do we miss?

Researcher owns the method: the search missions (Director orders them), their query portfolios, the maps of each
search space, the leads searches come across, the coverage audits, and the watched sources with their pace. It plans
weekly, or sooner when Director sends an order or feedback, when a mission's portfolio runs thin, or when a mission has
gone quiet. Its fast task is the search missions themselves (agent/research.py), which aim their exploring at the gaps
the maps name. It never reads mail, so it can check addresses on the web (isolation, docs/OPERATIONS.md).

Python gathers the facts, Claude judges, Python applies the result.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent import direction_ops as ops
from agent import search_map
from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import Settings
from agent.db import Database
from agent.memory import MemoryRepo
from agent.pipeline.outbox import add_candidate
from agent.posts import ROLES, Posts
from agent.prompts import prompt as prompt_template
from agent.research import BATCH, load_research
from agent.tools import build_web_tools

log = logging.getLogger(__name__)

LAST_KEY = "researcher.last_plan"
WORKING_MEMORY = "arbetsminne/researcher.md"
LOG_FILE = "research/log.md"
DEEP_KEY = "research.deep_requested.{id}"
PLAN_EVERY = timedelta(days=7)
QUIET_AFTER = timedelta(days=14)  # a mission with nothing kept this long is looked at (mechanics; the judgement is Researcher's)

SCHEMA = {
    "type": "object",
    "properties": {
        "missions": {
            "type": "array",
            "description": "Add, update or disable a search mission (orders come from Director)",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["add", "update", "disable"]},
                    "id": {"type": "string"},
                    "goal": {"type": ["string", "null"]},
                    "scope": {"type": ["string", "null"]},
                    "interval_days": {"type": ["number", "null"]},
                    "reason": {"type": "string"},
                },
                "required": ["op", "id", "reason"],
            },
        },
        "queries": {
            "type": "array",
            "description": "New queries for a mission's portfolio, aimed at its gaps",
            "items": {
                "type": "object",
                "properties": {"mission": {"type": "string"}, "query": {"type": "string"}, "rationale": {"type": "string"}},
                "required": ["mission", "query", "rationale"],
            },
        },
        "senses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["add_page", "add_feed", "add_company", "add_topic", "tune", "disable"]},
                    "name": {"type": "string", "description": "Id of the page/feed, company name, or the topic's name"},
                    "url": {"type": "string", "description": "URL of the page/feed or the company's careers page; add_topic: the search query; tune/disable: empty"},
                    "poll_minutes": {"type": ["number", "null"], "description": "tune: how often the sense is checked"},
                    "note": {"type": ["string", "null"], "description": "Swedish. What is worth something from this source (triage follows it)"},
                    "kind": {"type": ["string", "null"]},
                    "reason": {"type": "string"},
                },
                "required": ["op", "name", "reason"],
            },
        },
        "search_maps": {
            "type": "array",
            "description": "Only maps that changed: the whole new map of a mission's search space",
            "items": {
                "type": "object",
                "properties": {"mission": {"type": "string"}, "content_md": {"type": "string", "description": "Swedish"}},
                "required": ["mission", "content_md"],
            },
        },
        "lead_decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "decision": {"type": "string", "enum": ["add", "reject"]}, "reason": {"type": "string"}},
                "required": ["id", "decision", "reason"],
            },
        },
        "coverage_audit": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mission": {"type": "string"},
                    "benchmark": {"type": "array", "items": {"type": "string"}, "description": "Organisation or source names, 20-50"},
                    "note": {"type": "string", "description": "Swedish: how the benchmark was built"},
                },
                "required": ["mission", "benchmark", "note"],
            },
        },
        "deep_sweeps": {"type": "array", "items": {"type": "string"}, "description": "Missions that need one wide sweep soon"},
        "run_now": {"type": "array", "items": {"type": "string"}, "description": "Missions to run right away (an urgent order)"},
        "posts": {
            "type": "array",
            "description": "Reports or questions to another role (Director decides scope; questions for the owner go through Director)",
            "items": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "enum": ["director", "manager"]},
                    "kind": {"type": "string", "enum": ["note", "report", "question"]},
                    "body": {"type": "string", "description": "Swedish"},
                },
                "required": ["to", "kind", "body"],
            },
        },
        "owner_updates": {
            "type": "array",
            "description": "Rarely: something about the search the owner should know",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["difficulty", "success", "discovery"]},
                    "title": {"type": "string"},
                    "brief": {"type": "string", "description": "Swedish: what the owner should know, and what it means for them"},
                },
                "required": ["kind", "title", "brief"],
            },
        },
        "working_memory_md": {"type": "string", "description": "Swedish. Your whole new working memory"},
        "log": {"type": "string", "description": "Swedish. What you changed and why, briefly. Empty if nothing."},
    },
    "required": ["missions", "queries", "senses", "lead_decisions", "working_memory_md", "log"],
}


class ResearchPlanner:
    def __init__(self, settings: Settings, db: Database, gate: ClaudeGate, memory: MemoryRepo):
        self.settings = settings
        self.db = db
        self.gate = gate
        self.memory = memory
        self.tz = ZoneInfo(settings.timezone)
        self.posts = Posts(db)
        self.web = None  # WebReader, set by main
        self.start_mission: Callable[[str], None] | None = None  # set by main: run a mission now
        self._lock = asyncio.Lock()  # one Researcher at a time: its notebook has one writer
        self._seen_posts: list[int] = []
        self._chain_from_owner = False

    # --- when ---------------------------------------------------------------

    async def due(self) -> str | None:
        """Why a planning run is needed now, or None."""
        last = await self.db.kv_get(LAST_KEY)
        now = datetime.now(self.tz)
        if last is None or now - datetime.fromisoformat(last) >= PLAN_EVERY:
            return "weekly"
        if await self.posts.inbox("researcher"):
            return "inbox"
        for t in load_research():
            if not t.enabled:
                continue
            active = await self.db.fetchone("SELECT count(*) AS n FROM scout_queries WHERE mission = ? AND status = 'active'", (t.id,))
            if active["n"] < BATCH:
                return f"thin portfolio: {t.id}"
            st = await search_map.mission_stats(self.db, t.id)
            last_kept = datetime.fromisoformat(st["last_kept"] + "+00:00") if st["last_kept"] else None
            if st["runs_30d"] >= 3 and (last_kept is None or now - last_kept >= QUIET_AFTER) and now - datetime.fromisoformat(last) >= timedelta(days=3):
                return f"quiet mission: {t.id}"
        return None

    async def run_if_due(self) -> dict | None:
        if reason := await self.due():
            return await self.run(reason)
        return None

    # --- run ----------------------------------------------------------------

    async def run(self, trigger: str = "requested") -> dict | None:
        async with self._lock:
            return await self._run(trigger)

    async def _run(self, trigger: str) -> dict | None:
        now = datetime.now(self.tz)
        inbox = await self.posts.inbox("researcher")
        self._seen_posts = [p["id"] for p in inbox]
        self._chain_from_owner = any(p["from_owner"] for p in inbox)
        context = {
            "now": f"{now:%Y-%m-%d %H:%M} ({['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][now.weekday()]})",
            "trigger": trigger,
            "working_memory": self.memory.read(WORKING_MEMORY) or "(empty: this is your first run with a working memory)",
            "notes": Posts.format(inbox),
            "goals": self.memory.read("direction/goals.md", "direction/now.md", "interests.md") or "(Director has no goals written yet)",
            "missions": await self._missions(),
            "senses": await self._senses(),
        }
        tools = ["WebSearch", "Read(./memory/**)"]
        mcp: dict = {}
        if self.web is not None:
            w, names = build_web_tools(self.web)
            mcp, tools = {**mcp, **w}, tools + names
        try:
            result = await self.gate.run(
                ClaudeTask(
                    kind="researcher_plan",
                    prompt=prompt_template("researcher").format(**context),
                    priority=Priority.MANAGER,
                    model=self.settings.models.research,
                    allowed_tools=tools,
                    mcp_servers=mcp,
                    output_schema=SCHEMA,
                    max_turns=30,
                )
            )
        except QuotaPaused:
            log.info("researcher planning postponed: quota paused")
            return None
        if result.is_error or not isinstance(result.structured, dict):
            log.warning("researcher planning failed: %s", result.error)
            return None
        await self.apply(result.structured, now)
        await self.db.kv_set(LAST_KEY, now.isoformat())
        return result.structured

    async def apply(self, out: dict, now: datetime) -> list[str]:
        changes: list[str] = []
        if missions := out.get("missions"):
            changes += ops.apply_missions(missions)
        known = {t.id for t in load_research()}
        for q in out.get("queries") or []:
            if q["mission"] in known and q["query"].strip():
                await self.db.execute(
                    "INSERT OR IGNORE INTO scout_queries (mission, query, rationale) VALUES (?, ?, ?)", (q["mission"], q["query"].strip(), q.get("rationale", ""))
                )
        if n := len(out.get("queries") or []):
            changes.append(f"{n} nya sökfrågor")
        for s in out.get("senses") or []:
            if s["op"] == "tune" and s.get("poll_minutes"):
                changes.append(ops.tune_sense(s["name"], s["poll_minutes"], s["reason"]))
            elif s["op"] == "disable":
                changes.append(ops.disable_sense(s["name"], s["reason"]))
            elif not s.get("url"):
                continue
            elif s["op"] == "add_company":
                changes.append(await ops.add_company(s["name"], s["url"]))
            elif s["op"] == "add_topic":
                changes.append(ops.add_topic(s["name"], s["url"], "sv" if any(c in s["url"] for c in "åäö") else "en", s.get("note") or ""))
            else:
                changes.append(ops.add_page(s["name"], s["url"], s.get("note") or "", s.get("kind") or "event", "rss" if s["op"] == "add_feed" else "page"))
        for m in out.get("search_maps") or []:
            if m["mission"] not in known:
                continue  # maps belong to missions
            p = search_map.map_path(self.memory.path, m["mission"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(m["content_md"].strip() + "\n")
            changes.append(f"sökkarta {m['mission']}")
        changes += await search_map.decide_leads(self.db, out.get("lead_decisions") or [])
        for a in out.get("coverage_audit") or []:
            if a["mission"] in known:
                changes.append(await search_map.audit(
                    self.db, a["mission"], a["benchmark"], a.get("note", ""), (ops.SOURCES_FILE, ops.WATCHLIST_FILE, ops.RESEARCH_FILE)
                ))
        for mid in out.get("deep_sweeps") or []:
            if mid in known:
                await self.db.kv_set(DEEP_KEY.format(id=mid), now.isoformat())
                changes.append(f"djupsvep beställt: {mid}")
        for mid in out.get("run_now") or []:
            if mid in known and self.start_mission is not None:
                self.start_mission(mid)
                changes.append(f"körs nu: {mid}")
        for p in out.get("posts") or []:
            if p.get("to") in ROLES and p["to"] != "researcher":
                pid = await self.posts.send("researcher", p["to"], p.get("kind", "note"), p["body"])
                changes.append(f"post #P{pid} till {p['to']}")
        for u in (out.get("owner_updates") or [])[:1]:
            await add_candidate(
                self.db, kind="search_update", title=u["title"], body=u["brief"],
                features={"relevance": "high", "actionability": "medium" if u["kind"] == "difficulty" else "low", "time_sensitivity": "low",
                          "novelty": "high", "confidence": "high", "complement": "high", "reason": f"Researcher, sökningen: {u['kind']}"},
            )
            changes.append(f"läge om sökningen till ägaren: {u['title']}")
        if wm := (out.get("working_memory_md") or "").strip():
            path = self.memory.path / WORKING_MEMORY
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(wm + "\n")
        await self.posts.handled(self._seen_posts, f"läst av Researcher {now:%Y-%m-%d}")
        self._seen_posts = []
        log_text = (out.get("log") or "").strip()
        if log_text or changes:
            path = self.memory.path / LOG_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(f"\n## {now:%Y-%m-%d %H:%M}\n{log_text}\n" + "".join(f"- {c}\n" for c in changes))
        await self.memory.commit_if_changed(f"researcher {now:%Y-%m-%d %H:%M}")
        log.info("researcher planned: %s", "; ".join(changes) or "inga ändringar")
        return changes

    # --- context ----------------------------------------------------------------

    async def _missions(self) -> str:
        lines = []
        for t in load_research():
            lines.append(f"### {t.id} ({'on' if t.enabled else 'off'}, every {t.interval_days:g} days)\nGoal: {t.goal}\nScope: {t.scope}")
            if not t.enabled:
                continue
            q = await self.db.fetchall(
                "SELECT q.query, q.status, q.runs, (SELECT count(*) FROM events e JOIN candidates c ON c.event_id = e.id "
                "WHERE e.source = 'research:' || q.mission AND json_extract(e.payload, '$.query') = q.query "
                "AND c.status IN ('sent', 'shelved', 'pulled', 'scheduled')) AS kept FROM scout_queries q WHERE q.mission = ? ORDER BY q.status, kept DESC",
                (t.id,),
            )
            lines.append("Portfolio (status · kept/runs · query): " + ("; ".join(f"{x['status']} {x['kept']}/{x['runs']} \"{x['query']}\"" for x in q[:20]) or "empty"))
            lines.append(await search_map.deliveries(self.db, t.id))
            lines.append(await search_map.overview(self.db, self.memory.path, t.id))
        return "\n\n".join(lines) or "No missions yet."

    async def _senses(self) -> str:
        lines = [await self._sense_pace(), self._catalog()]
        if self.web is not None:
            lines.append("Reading per domain (method successes/attempts), hardest first:\n" + await self.web.stats())
        return "\n\n".join(line for line in lines if line)

    async def _sense_pace(self) -> str:
        """Per sense: how often it's checked, how often something new turns up, and whether it's worth anything."""
        from agent.sources.streams import load_stream_configs

        stats = {r["stream"]: r for r in await self.db.fetchall("SELECT * FROM stream_stats")}
        kept = {
            r["source"]: (r["n"], r["kept"])
            for r in await self.db.fetchall(
                "SELECT e.source, count(*) AS n, sum(c.status IN ('sent', 'shelved', 'pulled', 'scheduled')) AS kept FROM events e "
                "LEFT JOIN candidates c ON c.event_id = e.id WHERE COALESCE(e.triaged_at, '') != 'baseline' "
                "AND e.created_at >= datetime('now', '-14 days') GROUP BY e.source"
            )
        }
        total = sum(n for n, _ in kept.values()) or 1
        lines = ["Pace and yield per sense (id · checked every · new per day · errors · kept/in 14 d · share of everything in):"]
        for c in load_stream_configs():
            st = stats.get(c.id)
            days = max((datetime.now(ZoneInfo("UTC")) - datetime.fromisoformat(st["first_poll"] + "+00:00")).total_seconds() / 86400, 0.1) if st and st["first_poll"] else None
            n, k = kept.get(c.source, (0, 0))
            lines.append(
                f"- {c.id} ({c.type}{'' if c.enabled else ', OFF'}) · every {c.poll_minutes:g} min · "
                + (f"{st['new'] / days:.1f} new/day, {st['errors']} errors" if st and days else "no statistics yet")
                + (f" (last error: {(st['last_error'] or '')[:80]})" if st and st["errors"] else "")
                + f" · {k or 0}/{n} · {100 * n / total:.0f}%"
            )
        top = max(kept.items(), key=lambda x: x[1][0], default=None)
        if top and top[1][0] / total > 0.4:
            lines.append(f"Note: {top[0]} accounts for {100 * top[1][0] / total:.0f}% of everything coming in. The intake is one-sided.")
        return "\n".join(lines)

    def _catalog(self) -> str:
        """Verified sources not yet in use (config/source_catalog.yaml)."""
        import yaml

        from agent.config import ROOT
        from agent.sources.streams import load_stream_configs

        path = ROOT / "config" / "source_catalog.yaml"
        if not path.exists():
            return ""
        cat = yaml.safe_load(path.read_text()) or {}
        used = {c.url for c in load_stream_configs()} | {str(c.params.get("query")) for c in load_stream_configs()}
        lines = ["Source catalogue, verified but not in use (add_feed / add_topic / add_page):"]
        for group, feeds in (cat.get("feeds") or {}).items():
            free = [f for f in feeds if f["url"] not in used]
            if free:
                lines.append(f"- feeds, {group}: " + ", ".join(f"{f['name']} <{f['url']}>" for f in free))
        free_topics = [t for t in cat.get("topics") or [] if t["query"] not in used]
        if free_topics:
            lines.append("- topics: " + ", ".join(f"{t['name']} [{t['query']}, {t['lang']}]" for t in free_topics))
        free_pages = [p for p in cat.get("pages") or [] if p["url"] not in used]
        if free_pages:
            lines.append("- pages: " + ", ".join(f"{p['name']} <{p['url']}>" for p in free_pages))
        return "\n".join(lines)
