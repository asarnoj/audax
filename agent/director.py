"""Director: what matters for användaren, and what does the agent see that he doesn't? (docs/ARCHITECTURE.md)

Two loops, both in the background, never talking to the owner directly:
- understand (daily, Sonnet): keeps direction/ current (now, goals, threads) and steers missions and senses
- reflect (2-3x/week, Opus, high effort, web search): thinks deeply, checks ideas, and occasionally
  shares one through the outbox, where the dispatcher and Manager's policy still decide when

Python gathers the facts, Claude judges, Python applies the result: direction files, missions and senses
via direction_ops, questions via the inbox, ideas as outbox candidates.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent import direction_ops as ops
from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import Settings
from agent.db import Database
from agent.inbox import Inbox
from agent.memory import MemoryRepo
from agent.pipeline.outbox import add_candidate
from agent.prompts import prompt as prompt_template
from agent.research import load_research
from agent.schedule import Schedule, utc_iso
from agent.tools import build_github_tools, build_web_tools

log = logging.getLogger(__name__)

LAST_KEY = "director.last_{mode}"
LEVEL = {"type": "string", "enum": ["low", "medium", "high"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "person_md": {"type": ["string", "null"], "description": "Hela nya person.md, eller null om oförändrad"},
        "now_md": {"type": ["string", "null"], "description": "Hela nya now.md, eller null om oförändrad"},
        "goals_md": {"type": ["string", "null"], "description": "Hela nya goals.md, eller null"},
        "threads": {
            "type": "array",
            "description": "Bara trådar som ändrats",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "paused", "done", "dropped"]},
                    "content_md": {"type": "string"},
                },
                "required": ["slug", "status", "content_md"],
            },
        },
        "missions": {
            "type": "array",
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
        "senses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["add_page", "add_feed", "add_company", "add_topic", "tune", "disable"]},
                    "name": {"type": "string", "description": "id för sida/flöde, företagsnamn eller ämnets namn"},
                    "url": {"type": "string", "description": "sidans/flödets URL, företagets karriärsida, add_topic: sökfrågan, tune/disable: tomt"},
                    "poll_minutes": {"type": ["number", "null"], "description": "tune: hur ofta sinnet ska kollas"},
                    "note": {"type": ["string", "null"], "description": "vad som är värdefullt från källan"},
                    "kind": {"type": ["string", "null"]},
                    "reason": {"type": "string"},
                },
                "required": ["op", "name", "reason"],
            },
        },
        "questions": {
            "type": "array",
            "items": {"type": "object", "properties": {"what": {"type": "string"}, "why": {"type": "string"}}, "required": ["what", "why"]},
        },
        "ideas_md": {"type": ["string", "null"], "description": "Hela nya ideas.md, eller null"},
        "share": {
            "type": "array",
            "description": "Kontrollerade, verkligt värdefulla idéer till användaren. Oftast tom.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "message": {"type": "string", "description": "Utkast som chatten kan säga"},
                    "url": {"type": ["string", "null"]},
                    "why": {"type": "string"},
                    "time_sensitivity": LEVEL,
                    "expires": {"type": ["string", "null"], "description": "YYYY-MM-DD om idén har ett bäst-före"},
                },
                "required": ["title", "message", "why", "time_sensitivity"],
            },
        },
        "log": {"type": "string", "description": "Vad du ändrade och varför, kort. Tom om inget."},
    },
    "required": ["person_md", "now_md", "goals_md", "log"],
}
MEMORY_FILES = ("profile.md", "projects.md", "interests.md", "school.md", "schedule.md", "open-questions.md")


class Director:
    def __init__(self, settings: Settings, db: Database, gate: ClaudeGate, memory: MemoryRepo, schedule: Schedule):
        self.settings = settings
        self.db = db
        self.gate = gate
        self.memory = memory
        self.schedule = schedule
        self.inbox = Inbox(db)
        self.tz = ZoneInfo(settings.timezone)
        self.dir = memory.path / "direction"
        self.windows = None  # async () -> str, set when windows (GitHub, notes, ...) exist
        self.github = None  # read-only GitHubReader for digging deeper during reflection
        self.web = None  # WebReader

    # --- run --------------------------------------------------------------

    async def run(self, mode: str = "understand") -> dict | None:
        assert mode in ("understand", "reflect")
        now = datetime.now(self.tz)
        last = await self.db.kv_get(LAST_KEY.format(mode=mode))
        since = datetime.fromisoformat(last) if last else now - timedelta(days=7 if mode == "reflect" else 2)
        if mode == "reflect":
            since = min(since, now - timedelta(days=7))
        first_picture = self._person_empty()
        if first_picture:  # a first picture of a person needs the whole history, not just the latest messages
            since = now - timedelta(days=60)
        since_sql = since.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")

        context = {
            "now": f"{now:%Y-%m-%d %H:%M} ({['mån', 'tis', 'ons', 'tor', 'fre', 'lör', 'sön'][now.weekday()]})",
            "direction": self._direction(),
            "memory": self.memory.read(*MEMORY_FILES),
            "time": await self._time(now),
            "conversation": (
                "[person.md är tom. Bygg en första bild av personen utifrån hela historiken nedan och minnet ovan: "
                "det som är sagt med citat, och det du drar slutsatser om som hypoteser med belägg.]\n"
                if first_picture
                else ""
            )
            + await self._conversation(since_sql, full=(mode == "understand")),
            "notes": self._notes(await self.inbox.unprocessed("director")),
            "missions": await self._missions(),
            "windows": await self.windows() if self.windows else "Inga fönster kopplade än (GitHub, anteckningar och todo-sidan kommer).",
            "state": self.memory.read("state.md"),
        }
        if mode == "reflect":
            context["idea_feedback"] = await self._idea_feedback()
        reflect = mode == "reflect"
        tools = ["WebSearch", "Read(./memory/**)"] if reflect else ["Read(./memory/**)"]
        mcp: dict = {}
        if reflect and self.github is not None:
            gh, names = build_github_tools(self.github)
            mcp, tools = {**mcp, **gh}, tools + names
        if self.web is not None:  # both passes: the daily one checks new places too
            w, names = build_web_tools(self.web)
            mcp, tools = {**mcp, **w}, tools + names + ([] if reflect else ["WebSearch"])
        try:
            result = await self.gate.run(
                ClaudeTask(
                    kind=f"director_{mode}",
                    prompt=prompt_template(f"director_{mode}").format(**context),
                    priority=Priority.MANAGER,
                    model=self.settings.models.reflect if reflect else self.settings.models.director,
                    effort="high" if reflect else None,
                    allowed_tools=tools,
                    mcp_servers=mcp,
                    output_schema=SCHEMA,
                    max_turns=40 if reflect else 10,
                )
            )
        except QuotaPaused:
            log.info("director %s postponed: quota paused", mode)
            return None
        if result.is_error or not isinstance(result.structured, dict):
            log.warning("director %s failed: %s", mode, result.error)
            return None
        await self.apply(result.structured, mode, now)
        await self.db.kv_set(LAST_KEY.format(mode=mode), now.isoformat())
        return result.structured

    async def apply(self, out: dict, mode: str, now: datetime) -> list[str]:
        changes: list[str] = []
        self.dir.mkdir(exist_ok=True)
        (self.dir / "threads").mkdir(exist_ok=True)
        for name, key in (("person.md", "person_md"), ("now.md", "now_md"), ("goals.md", "goals_md"), ("ideas.md", "ideas_md")):
            if out.get(key):
                (self.dir / name).write_text(out[key].strip() + "\n")
                changes.append(f"{name} uppdaterad")
        for t in out.get("threads") or []:
            slug = ops.slug(t["slug"])
            text = t["content_md"].strip()
            if f"Status: {t['status']}" not in text:
                text = f"Status: {t['status']}\n\n{text}"
            (self.dir / "threads" / f"{slug}.md").write_text(text + "\n")
            changes.append(f"tråd {slug} ({t['status']})")

        if missions := out.get("missions"):
            changes += ops.apply_missions(missions)
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
                changes.append(
                    ops.add_page(s["name"], s["url"], s.get("note") or "", s.get("kind") or "event", "rss" if s["op"] == "add_feed" else "page")
                )

        for q in (out.get("questions") or [])[:1]:
            await self.inbox.request("director", q["what"], q["why"])
            changes.append(f"fråga: {q['what']}")
        for idea in (out.get("share") or [])[:2]:
            expires = None
            if idea.get("expires"):
                try:
                    expires = utc_iso(datetime.fromisoformat(idea["expires"][:10]).replace(hour=23, minute=59, tzinfo=self.tz))
                except ValueError:
                    pass
            await add_candidate(
                self.db,
                kind="insight",
                title=idea["title"],
                body=idea["message"],
                url=idea.get("url"),
                features={
                    "relevance": "high",
                    "actionability": "medium",
                    "time_sensitivity": idea.get("time_sensitivity", "low"),
                    "novelty": "high",
                    "confidence": "high",
                    "complement": "high",
                    "reason": f"Directors {mode}: {idea['why']}",
                },
                expires_at=expires,
            )
            changes.append(f"idé till användaren: {idea['title']}")

        await self.inbox.mark_processed("director")
        log_text = (out.get("log") or "").strip()
        if log_text or changes:
            with (self.dir / "log.md").open("a") as f:
                f.write(f"\n## {now:%Y-%m-%d %H:%M} · {mode}\n{log_text}\n" + "".join(f"- {c}\n" for c in changes))
        await self.memory.commit_if_changed(f"director {mode} {now:%Y-%m-%d %H:%M}")
        log.info("director %s: %s", mode, "; ".join(changes) or "inga ändringar")
        return changes

    # --- context ----------------------------------------------------------

    def _person_empty(self) -> bool:
        p = self.dir / "person.md"
        if not p.exists():
            return True
        text = re.sub(r"<!--.*?-->", "", p.read_text(), flags=re.DOTALL)
        return not any(line.strip().startswith(("-", "*")) or (line.strip() and not line.startswith("#")) for line in text.splitlines())

    def _direction(self) -> str:
        parts = [self.memory.read("direction/person.md", "direction/now.md", "direction/goals.md", "direction/ideas.md")]
        threads = sorted((self.dir / "threads").glob("*.md")) if (self.dir / "threads").exists() else []
        parts += [f"--- direction/threads/{t.name} ---\n{t.read_text().strip()}" for t in threads]
        return "\n\n".join(p for p in parts if p) or "Tomt."

    async def _time(self, now: datetime) -> str:
        """Where the owner's time went last week and where it's going, by kind of session."""
        past = await self.schedule.slots(now - timedelta(days=7), now)
        hours: dict[str, float] = defaultdict(float)
        for s in past:
            if not s.all_day:
                hours[s.summary] += (s.end - s.start).total_seconds() / 3600
        ahead = await self.schedule.slots(now, now + timedelta(days=14))
        lines = ["Senaste 7 dagarna (timmar):"] + [f"- {k}: {v:.1f}" for k, v in sorted(hours.items(), key=lambda x: -x[1])[:12]]
        lines.append("Kommande 14 dagar:")
        lines += [
            f"- {['mån', 'tis', 'ons', 'tor', 'fre', 'lör', 'sön'][s.start.weekday()]} {s.start:%d/%m %H:%M} {s.summary}" + (" [?]" if s.relevance == "unknown" else "")
            for s in ahead[:25]
        ]
        return "\n".join(lines)

    async def _conversation(self, since: str, full: bool) -> str:
        rows = await self.db.fetchall("SELECT direction, origin, text, created_at FROM messages WHERE created_at >= ? ORDER BY id", (since,))
        limit = 1500 if full else 400
        lines = []
        for r in rows[-120:]:
            who = "användaren" if r["direction"] == "in" else "agenten"
            text = r["text"] if len(r["text"]) <= limit else r["text"][:limit] + "…"
            d = datetime.fromisoformat(r["created_at"] + "+00:00").astimezone(self.tz)
            lines.append(f"{d:%d/%m %H:%M} {who}: {text}")
        return "\n".join(lines) or "Inget."

    @staticmethod
    def _notes(notes: list[dict]) -> str:
        return "\n".join(
            f"- \"{n['verbatim']}\" → {n['interpretation']}" + (f" (svar på: {n['question']})" if n.get("question") else "")
            for n in notes
        ) or "Inga."

    async def _missions(self) -> str:
        lines = ["Uppdrag (instance/config/research.yaml):"]
        for t in load_research():
            q = await self.db.fetchall(
                "SELECT q.query, q.status, q.runs, (SELECT count(*) FROM events e JOIN candidates c ON c.event_id = e.id "
                "WHERE e.source = 'research:' || q.mission AND json_extract(e.payload, '$.query') = q.query "
                "AND c.status IN ('sent', 'shelved', 'pulled')) AS kept FROM scout_queries q WHERE q.mission = ?",
                (t.id,),
            )
            best = sorted((x for x in q if x["status"] == "active"), key=lambda x: x["kept"], reverse=True)[:3]
            lines.append(
                f"- {t.id} ({'på' if t.enabled else 'av'}, var {t.interval_days:g}:e dag): {t.goal} · scope: {t.scope}"
                + (" · bästa frågor: " + ", ".join(f"\"{x['query']}\" ({x['kept']}/{x['runs']})" for x in best) if best else "")
            )
        rows = await self.db.fetchall(
            "SELECT e.source, count(*) AS n, sum(c.status = 'sent') AS sent, sum(c.status IN ('shelved', 'pulled')) AS shelf, "
            "sum(c.status = 'dropped') AS dropped FROM events e LEFT JOIN candidates c ON c.event_id = e.id "
            "WHERE e.created_at >= datetime('now', '-7 days') AND COALESCE(e.triaged_at, '') != 'baseline' GROUP BY e.source ORDER BY n DESC"
        )
        lines.append("Sinnen senaste 7 dagarna (in → skickat / hylla / bortsorterat):")
        lines += [f"- {r['source']}: {r['n']} → {r['sent'] or 0} / {r['shelf'] or 0} / {r['dropped'] or 0}" for r in rows] or ["- inga"]
        lines.append(await self._sense_pace())
        lines.append(self._catalog())
        if self.web is not None:
            lines.append("Läsning per domän (metod lyckade/försök), svårast först:\n" + await self.web.stats())
        return "\n".join(lines)

    async def _sense_pace(self) -> str:
        """Per sense: how often it's checked, how often something new turns up, and whether it's worth anything."""
        from agent.sources.streams import load_stream_configs

        stats = {r["stream"]: r for r in await self.db.fetchall("SELECT * FROM stream_stats")}
        kept = {
            r["source"]: (r["n"], r["kept"])
            for r in await self.db.fetchall(
                "SELECT e.source, count(*) AS n, sum(c.status IN ('sent', 'shelved', 'pulled')) AS kept FROM events e "
                "LEFT JOIN candidates c ON c.event_id = e.id WHERE COALESCE(e.triaged_at, '') != 'baseline' "
                "AND e.created_at >= datetime('now', '-14 days') GROUP BY e.source"
            )
        }
        total = sum(n for n, _ in kept.values()) or 1
        lines = ["Sinnenas takt och utbyte (id · kollas var · nya per dygn · behållna/in 14 d · andel av allt in):"]
        for c in load_stream_configs():
            st = stats.get(c.id)
            days = max((datetime.now(ZoneInfo("UTC")) - datetime.fromisoformat(st["first_poll"] + "+00:00")).total_seconds() / 86400, 0.1) if st else None
            n, k = kept.get(c.source, (0, 0))
            lines.append(
                f"- {c.id} ({c.type}{'' if c.enabled else ', AV'}) · var {c.poll_minutes:g} min · "
                + (f"{st['new'] / days:.1f} nya/dygn, {st['errors']} fel" if st else "ingen statistik än")
                + f" · {k or 0}/{n} · {100 * n / total:.0f}%"
            )
        top = max(kept.items(), key=lambda x: x[1][0], default=None)
        if top and top[1][0] / total > 0.4:
            lines.append(f"OBS: {top[0]} står för {100 * top[1][0] / total:.0f}% av allt som kommer in. Underlaget är ensidigt.")
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
        lines = ["Källkatalog, verifierade men inte i bruk (add_feed / add_topic / add_page):"]
        for group, feeds in (cat.get("feeds") or {}).items():
            free = [f for f in feeds if f["url"] not in used]
            if free:
                lines.append(f"- flöden, {group}: " + ", ".join(f"{f['name']} <{f['url']}>" for f in free))
        free_topics = [t for t in cat.get("topics") or [] if t["query"] not in used]
        if free_topics:
            lines.append("- ämnen: " + ", ".join(f"{t['name']} [{t['query']}, {t['lang']}]" for t in free_topics))
        free_pages = [p for p in cat.get("pages") or [] if p["url"] not in used]
        if free_pages:
            lines.append("- sidor: " + ", ".join(f"{p['name']} <{p['url']}>" for p in free_pages))
        return "\n".join(lines)

    async def _idea_feedback(self) -> str:
        rows = await self.db.fetchall(
            "SELECT c.title, c.status, (SELECT group_concat(f.signal || ':' || f.value, ', ') FROM feedback f "
            "WHERE f.candidate_id = c.id OR f.message_id IN (SELECT message_id FROM message_candidates WHERE candidate_id = c.id)) AS fb "
            "FROM candidates c WHERE c.kind = 'insight' ORDER BY c.id DESC LIMIT 15"
        )
        return "\n".join(f"- {r['title']}: {r['status']}, {r['fb'] or 'ingen reaktion'}" for r in rows) or "Inga delade idéer än."
