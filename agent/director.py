"""Director: what matters to the owner, and what does Audax see that they don't? (docs/ARCHITECTURE.md)

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
from agent.tools import build_github_tools, build_mail_tools, build_web_tools

log = logging.getLogger(__name__)

LAST_KEY = "director.last_{mode}"
PROPOSED_KEY = "director.proposed_sources"  # URL sources from a review that read mail, for the reflection to check
URL_OPS = ("add_page", "add_feed", "add_company")
LEVEL = {"type": "string", "enum": ["low", "medium", "high"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "person_md": {"type": ["string", "null"], "description": "Swedish. The whole new person.md, or null if unchanged"},
        "now_md": {"type": ["string", "null"], "description": "Swedish. The whole new now.md, or null if unchanged"},
        "goals_md": {"type": ["string", "null"], "description": "Swedish. The whole new goals.md, or null"},
        "threads": {
            "type": "array",
            "description": "Only threads that changed",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "paused", "done", "dropped"]},
                    "content_md": {"type": "string", "description": "Swedish. The whole thread file"},
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
        "questions": {
            "type": "array",
            "description": "At most one, in Swedish",
            "items": {"type": "object", "properties": {"what": {"type": "string"}, "why": {"type": "string"}}, "required": ["what", "why"]},
        },
        "ideas_md": {"type": ["string", "null"], "description": "Swedish. The whole new ideas.md, or null"},
        "share": {
            "type": "array",
            "description": "Reflection only: checked, truly valuable ideas for the owner. Usually empty.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "brief": {"type": "string", "description": "Swedish. What it is and why it fits the owner; the chat writes the message"},
                    "url": {"type": ["string", "null"]},
                    "why": {"type": "string", "description": "Swedish, one sentence; shown if the owner asks why"},
                    "time_sensitivity": LEVEL,
                    "expires": {"type": ["string", "null"], "description": "YYYY-MM-DD if the idea has a best-before date"},
                },
                "required": ["title", "brief", "why", "time_sensitivity"],
            },
        },
        "log": {"type": "string", "description": "Swedish. What you changed and why, briefly. Empty if nothing."},
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
        self.mail = None  # read-only MailReader: the daily review only (isolation, see run())

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
            "now": f"{now:%Y-%m-%d %H:%M} ({['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][now.weekday()]})",
            "direction": self._direction(),
            "memory": self.memory.read(*MEMORY_FILES),
            "time": await self._time(now),
            "conversation": (
                "[person.md is empty. Build a first picture of the person from the whole history below and the memory above: "
                "what was said, with quotes, and what you infer, as hypotheses with evidence.]\n"
                if first_picture
                else ""
            )
            + await self._conversation(since_sql, full=(mode == "understand")),
            "notes": self._notes(await self.inbox.unprocessed("director")),
            "missions": await self._missions(),
            "windows": await self._windows(mode),
            "state": self.memory.read("state.md"),
        }
        if mode == "reflect":
            context["idea_feedback"] = await self._idea_feedback()
            context["proposed"] = self._proposed_text(await self.db.kv_get(PROPOSED_KEY) or [])
        reflect = mode == "reflect"
        # Isolation: a run that reads mail never reaches new web addresses (so a mail can't make it leak data in a
        # URL). With mail connected, the daily review reads mail and has no web_read; the reflection has the web and
        # never sees mail. WebSearch only sends a query to the search provider, so both keep it.
        isolated = not reflect and self.mail is not None
        tools = ["WebSearch", "Read(./memory/**)"] if reflect or self.web is not None else ["Read(./memory/**)"]
        mcp: dict = {}
        if reflect and self.github is not None:
            gh, names = build_github_tools(self.github)
            mcp, tools = {**mcp, **gh}, tools + names
        if self.web is not None and not isolated:
            w, names = build_web_tools(self.web)
            mcp, tools = {**mcp, **w}, tools + names
        if isolated:
            m, names = build_mail_tools(self.mail)
            mcp, tools = {**mcp, **m}, tools + names
        try:
            result = await self.gate.run(
                ClaudeTask(
                    kind=f"director_{mode}",
                    prompt=(prompt_template("director") + "\n\n" + prompt_template(f"director_{mode}")).format(**context),
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
        if reflect:
            await self.db.kv_set(PROPOSED_KEY, None)  # checked (added or let go) by this reflection
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
        deferred = []
        for s in out.get("senses") or []:
            if mode == "understand" and self.mail is not None and s["op"] in URL_OPS and s.get("url"):
                deferred.append(s)  # this run read mail: the reflection checks the address and adds it
                changes.append(f"föreslagen källa {s['name']} <{s['url']}>, kontrolleras i reflektionen")
                continue
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

        if deferred:
            await self.db.kv_set(PROPOSED_KEY, ((await self.db.kv_get(PROPOSED_KEY)) or []) + deferred)
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
                body=idea.get("brief") or idea.get("message", ""),
                url=idea.get("url"),
                features={
                    "relevance": "high",
                    "actionability": "medium",
                    "time_sensitivity": idea.get("time_sensitivity", "low"),
                    "novelty": "high",
                    "confidence": "high",
                    "complement": "high",
                    "reason": f"Director ({mode}): {idea['why']}",
                },
                expires_at=expires,
            )
            changes.append(f"idé till ägaren: {idea['title']}")

        await self.inbox.mark_processed("director")
        log_text = (out.get("log") or "").strip()
        if log_text or changes:
            with (self.dir / "log.md").open("a") as f:
                f.write(f"\n## {now:%Y-%m-%d %H:%M} · {mode}\n{log_text}\n" + "".join(f"- {c}\n" for c in changes))
        await self.memory.commit_if_changed(f"director {mode} {now:%Y-%m-%d %H:%M}")
        log.info("director %s: %s", mode, "; ".join(changes) or "inga ändringar")
        return changes

    # --- context ----------------------------------------------------------

    async def _windows(self, mode: str) -> str:
        parts = [await self.windows() if self.windows else "No windows connected yet."]
        if mode == "understand" and self.mail is not None:
            try:
                parts.append(await self.mail.window())
            except Exception as e:  # a broken mail token must not stop the Director
                log.warning("mail window failed: %s", e)
                parts.append(f"Mail: couldn't be read this time ({type(e).__name__}).")
        return "\n\n".join(parts)

    @staticmethod
    def _proposed_text(items: list[dict]) -> str:
        return "\n".join(f"- {s['op']} {s['name']} <{s['url']}>: {s.get('reason', '')}" for s in items) or "None."

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
        return "\n\n".join(p for p in parts if p) or "Empty."

    async def _time(self, now: datetime) -> str:
        """Where the owner's time went last week and where it's going, by kind of session."""
        past = await self.schedule.slots(now - timedelta(days=7), now)
        hours: dict[str, float] = defaultdict(float)
        for s in past:
            if not s.all_day:
                hours[s.summary] += (s.end - s.start).total_seconds() / 3600
        ahead = await self.schedule.slots(now, now + timedelta(days=14))
        lines = ["Last 7 days (hours):"] + [f"- {k}: {v:.1f}" for k, v in sorted(hours.items(), key=lambda x: -x[1])[:12]]
        lines.append("Next 14 days:")
        lines += [
            f"- {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][s.start.weekday()]} {s.start:%d/%m %H:%M} {s.summary}" + (" [?]" if s.relevance == "unknown" else "")
            for s in ahead[:25]
        ]
        return "\n".join(lines)

    async def _conversation(self, since: str, full: bool) -> str:
        rows = await self.db.fetchall("SELECT direction, origin, text, created_at FROM messages WHERE created_at >= ? ORDER BY id", (since,))
        limit = 1500 if full else 400
        lines = []
        for r in rows[-120:]:
            who = "owner" if r["direction"] == "in" else "Audax"
            text = r["text"] if len(r["text"]) <= limit else r["text"][:limit] + "…"
            d = datetime.fromisoformat(r["created_at"] + "+00:00").astimezone(self.tz)
            lines.append(f"{d:%d/%m %H:%M} {who}: {text}")
        return "\n".join(lines) or "Nothing."

    @staticmethod
    def _notes(notes: list[dict]) -> str:
        return "\n".join(
            f"- \"{n['verbatim']}\" → {n['interpretation']}" + (f" (answer to: {n['question']})" if n.get("question") else "")
            for n in notes
        ) or "None."

    async def _missions(self) -> str:
        lines = ["Search missions:"]
        for t in load_research():
            q = await self.db.fetchall(
                "SELECT q.query, q.status, q.runs, (SELECT count(*) FROM events e JOIN candidates c ON c.event_id = e.id "
                "WHERE e.source = 'research:' || q.mission AND json_extract(e.payload, '$.query') = q.query "
                "AND c.status IN ('sent', 'shelved', 'pulled')) AS kept FROM scout_queries q WHERE q.mission = ?",
                (t.id,),
            )
            best = sorted((x for x in q if x["status"] == "active"), key=lambda x: x["kept"], reverse=True)[:3]
            lines.append(
                f"- {t.id} ({'on' if t.enabled else 'off'}, every {t.interval_days:g} days): {t.goal} · scope: {t.scope}"
                + (" · best queries: " + ", ".join(f"\"{x['query']}\" ({x['kept']}/{x['runs']})" for x in best) if best else "")
            )
        rows = await self.db.fetchall(
            "SELECT e.source, count(*) AS n, sum(c.status = 'sent') AS sent, sum(c.status IN ('shelved', 'pulled')) AS shelf, "
            "sum(c.status = 'dropped') AS dropped FROM events e LEFT JOIN candidates c ON c.event_id = e.id "
            "WHERE e.created_at >= datetime('now', '-7 days') AND COALESCE(e.triaged_at, '') != 'baseline' GROUP BY e.source ORDER BY n DESC"
        )
        lines.append("Senses, last 7 days (in → sent / shelf / dropped):")
        lines += [f"- {r['source']}: {r['n']} → {r['sent'] or 0} / {r['shelf'] or 0} / {r['dropped'] or 0}" for r in rows] or ["- none"]
        lines.append(await self._sense_pace())
        lines.append(self._catalog())
        if self.web is not None:
            lines.append("Reading per domain (method successes/attempts), hardest first:\n" + await self.web.stats())
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
        lines = ["Pace and yield per sense (id · checked every · new per day · kept/in 14 d · share of everything in):"]
        for c in load_stream_configs():
            st = stats.get(c.id)
            days = max((datetime.now(ZoneInfo("UTC")) - datetime.fromisoformat(st["first_poll"] + "+00:00")).total_seconds() / 86400, 0.1) if st else None
            n, k = kept.get(c.source, (0, 0))
            lines.append(
                f"- {c.id} ({c.type}{'' if c.enabled else ', OFF'}) · every {c.poll_minutes:g} min · "
                + (f"{st['new'] / days:.1f} new/day, {st['errors']} errors" if st else "no statistics yet")
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

    async def _idea_feedback(self) -> str:
        rows = await self.db.fetchall(
            "SELECT c.title, c.status, (SELECT group_concat(f.signal || ':' || f.value, ', ') FROM feedback f "
            "WHERE f.candidate_id = c.id OR f.message_id IN (SELECT message_id FROM message_candidates WHERE candidate_id = c.id)) AS fb "
            "FROM candidates c WHERE c.kind = 'insight' ORDER BY c.id DESC LIMIT 15"
        )
        return "\n".join(f"- {r['title']}: {r['status']}, {r['fb'] or 'no reaction'}" for r in rows) or "No shared ideas yet."
