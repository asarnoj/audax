"""Director: what matters to the owner, and what does Audax see that they don't? (docs/ARCHITECTURE.md)

Two loops, both in the background, never talking to the owner directly:
- understand (daily, Sonnet): keeps direction/ current (now, goals, threads) and steers missions and senses
- reflect (2-3x/week, Opus, high effort, web search): thinks deeply, checks ideas, and occasionally
  shares one through the outbox, where the dispatcher and Manager's policy still decide when

Python gathers the facts, Claude judges, Python applies the result: direction files, missions and senses
via direction_ops, posts to the other roles and questions for the owner via the internal mail (agent/posts.py),
ideas as outbox candidates. Its working memory (memory/arbetsminne/director.md) is what makes it the same mind
from night to night: read first, rewritten every run (docs/MODEL.md).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent import direction_ops as ops
from agent import search_map
from agent.pursuits import Pursuits
from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import Settings
from agent.db import Database
from agent.posts import ROLES, Posts
from agent.memory import MemoryRepo
from agent.pipeline.outbox import add_candidate
from agent.prompts import prompt as prompt_template
from agent.research import load_research
from agent.schedule import Schedule, utc_iso
from agent.tools import build_github_tools, build_mail_tools, build_web_tools

log = logging.getLogger(__name__)

LAST_KEY = "director.last_{mode}"
WORKING_MEMORY = "arbetsminne/director.md"
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
        "questions": {
            "type": "array",
            "description": "At most one, in Swedish",
            "items": {"type": "object", "properties": {"what": {"type": "string"}, "why": {"type": "string"}}, "required": ["what", "why"]},
        },
        "posts": {
            "type": "array",
            "description": "Notes, orders or feedback to another role (not to the owner). They read it at their next run.",
            "items": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "enum": ["manager", "researcher"]},
                    "kind": {"type": "string", "enum": ["note", "order", "feedback"]},
                    "body": {"type": "string", "description": "Swedish"},
                    "urgent": {"type": "boolean", "description": "Start the recipient now; only works when the owner's words started this"},
                },
                "required": ["to", "kind", "body"],
            },
        },
        "working_memory_md": {"type": "string", "description": "Swedish. Your whole new working memory (see the prompt)"},
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
        "pursuit_updates": {
            "type": "array",
            "description": "Keep what the owner pursues true: add missing ones, update status, deadline, next step, reminder day",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": ["integer", "null"], "description": "Existing pursuit id; null to add"},
                    "title": {"type": "string"},
                    "organization": {"type": ["string", "null"]},
                    "url": {"type": ["string", "null"]},
                    "deadline": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                    "status": {"type": "string", "enum": ["interested", "preparing", "applied", "interviewing", "offer", "accepted", "rejected", "declined"]},
                    "next_step": {"type": ["string", "null"], "description": "Swedish, short"},
                    "remind_at": {"type": ["string", "null"], "description": "YYYY-MM-DD: when a nudge would help most"},
                    "note": {"type": ["string", "null"], "description": "Swedish: what changed and how you know"},
                },
                "required": ["title", "status"],
            },
        },
        "log": {"type": "string", "description": "Swedish. What you changed and why, briefly. Empty if nothing."},
    },
    "required": ["person_md", "now_md", "goals_md", "pursuit_updates", "working_memory_md", "log"],
}
MEMORY_FILES = ("profile.md", "projects.md", "interests.md", "school.md", "schedule.md")


class Director:
    def __init__(self, settings: Settings, db: Database, gate: ClaudeGate, memory: MemoryRepo, schedule: Schedule):
        self.settings = settings
        self.db = db
        self.gate = gate
        self.memory = memory
        self.schedule = schedule
        self.posts = Posts(db)
        self._lock = asyncio.Lock()  # one Director at a time: its notebook has one writer
        self._seen_posts: list[int] = []
        self._chain_from_owner = False
        self.tz = ZoneInfo(settings.timezone)
        self.dir = memory.path / "direction"
        self.windows = None  # async () -> str, set when windows (GitHub, notes, ...) exist
        self.github = None  # read-only GitHubReader for digging deeper during reflection
        self.web = None  # WebReader
        self.mail = None  # read-only MailReader: the daily review only (isolation, see run())

    # --- run --------------------------------------------------------------

    async def run(self, mode: str = "understand") -> dict | None:
        assert mode in ("understand", "reflect")
        async with self._lock:
            return await self._run(mode)

    async def _run(self, mode: str) -> dict | None:
        now = datetime.now(self.tz)
        last = await self.db.kv_get(LAST_KEY.format(mode=mode))
        since = datetime.fromisoformat(last) if last else now - timedelta(days=7 if mode == "reflect" else 2)
        if mode == "reflect":
            since = min(since, now - timedelta(days=7))
        first_picture = self._person_empty()
        if first_picture:  # a first picture of a person needs the whole history, not just the latest messages
            since = now - timedelta(days=60)
        since_sql = since.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")

        inbox = await self.posts.inbox("director")
        self._seen_posts = [p["id"] for p in inbox]
        self._chain_from_owner = any(p["from_owner"] for p in inbox)
        context = {
            "now": f"{now:%Y-%m-%d %H:%M} ({['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][now.weekday()]})",
            "working_memory": self.memory.read(WORKING_MEMORY) or "(empty: this is your first run with a working memory)",
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
            "notes": Posts.format(inbox),
            "missions": await self._search_report(),
            "windows": await self._windows(mode),
            "state": self.memory.read("state.md"),
            "pursuits": await Pursuits(self.db, self.settings.timezone).summary() + "\n" + await Pursuits(self.db).stats(),
        }
        if mode == "reflect":
            context["idea_feedback"] = await self._idea_feedback()
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

        pursuits = Pursuits(self.db, self.settings.timezone)
        for u in out.get("pursuit_updates") or []:
            try:
                pid, created = await pursuits.upsert(u, "director")
                changes.append(f"{'nytt spår' if created else 'spår'} #{pid}: {u['title']} ({u['status']})")
            except ValueError:
                continue
        for q in (out.get("questions") or [])[:1]:
            await self.posts.send("director", "assistant", "question", q["what"], context=q["why"])
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

        for p in out.get("posts") or []:
            if p.get("to") in ROLES and p["to"] != "director":
                pid = await self.posts.send(
                    "director", p["to"], p.get("kind", "note"), p["body"], from_owner=self._chain_from_owner, wake=bool(p.get("urgent")),
                )
                changes.append(f"post #P{pid} till {p['to']}")
        if wm := (out.get("working_memory_md") or "").strip():
            path = self.memory.path / WORKING_MEMORY
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(wm + "\n")
        await self.posts.handled(self._seen_posts, f"läst av Director ({mode} {now:%Y-%m-%d})")
        self._seen_posts = []
        log_text = (out.get("log") or "").strip()
        if log_text or changes:
            with (self.dir / "log.md").open("a") as f:
                f.write(f"\n## {now:%Y-%m-%d %H:%M} · {mode}\n{log_text}\n" + "".join(f"- {c}\n" for c in changes))
        await self.memory.commit_if_changed(f"director {mode} {now:%Y-%m-%d %H:%M}", by="director")
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


    async def _search_report(self) -> str:
        """What Director ordered from Researcher, and what each mission delivered: the basis for feedback."""
        lines = []
        for t in load_research():
            if not t.enabled:
                continue
            lines.append(f"### {t.id}\nGoal: {t.goal}\nScope: {t.scope}")
            lines.append(await search_map.deliveries(self.db, t.id))
            cov = await self.db.fetchone("SELECT created_at, recall FROM coverage WHERE mission = ? ORDER BY id DESC LIMIT 1", (t.id,))
            if cov and cov["recall"] is not None:
                lines.append(f"Researcher's latest coverage audit ({cov['created_at'][:10]}): {cov['recall']:.0%} of an independent benchmark reached.")
        return "\n\n".join(lines) or "No missions yet."

    async def _idea_feedback(self) -> str:
        rows = await self.db.fetchall(
            "SELECT c.title, c.status, (SELECT group_concat(f.signal || ':' || f.value, ', ') FROM feedback f "
            "WHERE f.candidate_id = c.id OR f.message_id IN (SELECT message_id FROM message_candidates WHERE candidate_id = c.id)) AS fb "
            "FROM candidates c WHERE c.kind = 'insight' ORDER BY c.id DESC LIMIT 15"
        )
        return "\n".join(f"- {r['title']}: {r['status']}, {r['fb'] or 'no reaction'}" for r in rows) or "No shared ideas yet."
