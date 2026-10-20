"""Manager: owner of the experience (docs/MODEL.md, docs/archive/ATTENTION.md §4.6).

Nightly: reviews the conversation like a UX researcher, calibrates pacing, voice and what the situation means for
the owner's attention (all in policy.md), and tends the shelf. What the agent looks for is Director's job, not
Manager's.

Python gathers the facts, Claude judges, Python applies the result within fixed bounds:
- policy.md is written here (the chat can't edit it); the run's own account is kept on the run (agent/trail.py)
- structural changes to the experience become proposals that the owner approves with a button
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


from agent import trail
from agent.due import answered_since, review
from agent.items import Items
from agent.office_tools import build_office_tools, describe_forms
from agent.pursuits import Pursuits
from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import Settings
from agent.db import Database
from agent.posts import ROLES, Posts
from agent.memory import MemoryRepo
from agent.pipeline.outbox import add_candidate
from agent.prompts import prompt as prompt_template

log = logging.getLogger(__name__)

MODULE = "manager@1"
LAST_RUN_KEY = "manager.last_run"
WORKING_MEMORY = "arbetsminne/manager.md"
CHECKIN_KEY = "manager.last_checkin"

SCHEMA = {
    "type": "object",
    "properties": {
        "policy_md": {"type": ["string", "null"], "description": "Swedish. The whole new policy.md, or null if unchanged"},
        "policy_reason": {"type": "string", "description": "Swedish, one sentence"},
        "resurface": {"type": "array", "items": {"type": "integer"}, "description": "Candidate ids from the shelf to bring back"},
        "proposal": {
            "type": ["object", "null"],
            "properties": {
                "kind": {"type": "string", "enum": ["other"]},
                "title": {"type": "string"},
                "description": {"type": "string", "description": "Swedish, to the owner: what and why, 1-3 sentences"},
            },
            "required": ["kind", "title", "description"],
        },
        "checkin": {"type": "boolean", "description": "Send the check-in question now"},
        "lessons": {"type": "array", "items": {"type": "string"}, "description": "Swedish: lessons for the whole office; usually none"},
        "posts": {
            "type": "array",
            "description": "Notes or feedback to another role (not to the owner). They read it at their next run.",
            "items": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "enum": ["director", "researcher"]},
                    "kind": {"type": "string", "enum": ["note", "feedback"]},
                    "body": {"type": "string", "description": "Swedish"},
                },
                "required": ["to", "kind", "body"],
            },
        },
        "agenda_changes": {
            "type": "array",
            "description": "Move or cancel planned contact (#F planned deliveries, #B bookings). The owner's own bookings are fixed.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "#F210 or #B12"},
                    "action": {"type": "string", "enum": ["move", "cancel"]},
                    "at": {"type": ["string", "null"], "description": "move: local time, YYYY-MM-DD HH:MM"},
                    "reason": {"type": "string", "description": "Swedish"},
                },
                "required": ["id", "action", "reason"],
            },
        },
        "working_memory_md": {"type": "string", "description": "Swedish. Your whole new working memory (see the prompt)"},
        "summary": {"type": "string", "description": "Swedish. What you saw and what you changed, and why: the run's own account"},
        "note_to_owner": {"type": ["string", "null"], "description": "Swedish brief for the chat, or null"},
        "desk_review": {
            "type": "object",
            "description": "Your spot check of the desk's (the dispatcher's) decisions since the last run",
            "properties": {
                "sampled": {"type": "integer", "description": "How many decisions you checked"},
                "followed_policy": {"type": "boolean"},
                "notes": {"type": "string", "description": "Swedish: where it drifted from the policy, if anywhere, and what you changed"},
            },
            "required": ["sampled", "followed_policy", "notes"],
        },
    },
    "required": ["policy_md", "policy_reason", "working_memory_md", "desk_review", "summary", "checkin"],
}

CHECKIN_QUESTION = "Avstämning: hur har mängden meddelanden från mig känts senaste tiden?"
CHECKIN_OPTIONS = ["För mycket", "Lagom", "För lite", "Fel saker, inte fel mängd"]


class Manager:
    def __init__(self, settings: Settings, db: Database, gate: ClaudeGate, memory: MemoryRepo):
        self.settings = settings
        self.db = db
        self.gate = gate
        self.memory = memory
        self.tz = ZoneInfo(settings.timezone)
        self.posts = Posts(db)
        self.agenda = None  # set by main: the schedule Manager owns (agent/agenda.py)
        self._lock = asyncio.Lock()  # one Manager at a time: its notebook has one writer
        self._seen_posts: list[int] = []

    def now(self) -> datetime:
        return datetime.now(self.tz)

    # --- run ----------------------------------------------------------------

    async def run(self) -> dict | None:
        async with self._lock:
            return await self._run()

    async def _run(self) -> dict | None:
        now = self.now()
        inbox = await self.posts.inbox("manager")
        self._seen_posts = [p["id"] for p in inbox]
        last = await self.db.kv_get(LAST_RUN_KEY)
        since = datetime.fromisoformat(last) if last else now - timedelta(days=1)
        since_sql = since.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")

        prompt = prompt_template("manager").format(
            now=f"{now:%Y-%m-%d %H:%M} ({['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][now.weekday()]})",
            since=f"{since:%Y-%m-%d %H:%M}",
            working_memory=self.memory.read(WORKING_MEMORY) or "(empty: this is your first run with a working memory)",
            situation=self.memory.read("direction/now.md") or "(Director has no picture yet)",
            planned=await self.agenda.upcoming(7) if self.agenda else "(no schedule)",
            desk=await self._desk(since_sql),
            measures=await self._measures(),
            policy=self.memory.read("policy.md", "lardomar.md"),
            experiments=await self._experiments(),
            review=await review(self.db, "manager", now),
            answers=await self._answers(last),
            forms=describe_forms("manager"),
            activity=await self._activity(since_sql),
            feedback=await self._feedback(since_sql),
            conversation=await self._conversation(since_sql),
            notes=Posts.format(inbox),
            shelf=await self._shelf(),
            proposals=await self._proposals(),
            misc=await self._misc(),
        )
        office, office_names = build_office_tools(self.db, "manager")   # experiments, beliefs, questions, steps
        try:
            result = await self.gate.run(
                ClaudeTask(
                    kind="manager",
                    prompt=prompt,
                    priority=Priority.MANAGER,
                    model=self.settings.models.manager,
                    allowed_tools=["Read(./memory/**)", *office_names],  # its files are written by Python from its answer
                    mcp_servers=office,
                    output_schema=SCHEMA,
                    max_turns=35,
                )
            )
        except QuotaPaused:
            log.info("manager postponed: quota paused")
            return None
        if result.is_error or not isinstance(result.structured, dict):
            log.warning("manager failed: %s", result.error)
            return None

        out = result.structured
        await self.apply(out, now)
        await self.db.kv_set(LAST_RUN_KEY, now.isoformat())
        return out

    async def apply(self, out: dict, now: datetime) -> None:
        changes: list[str] = []

        if out.get("policy_md"):
            (self.memory.path / "policy.md").write_text(out["policy_md"].strip() + "\n")
            changes.append(f"policy: {out.get('policy_reason', '')}")
        for cid in out.get("resurface") or []:
            if await self.db.fetchone("SELECT 1 FROM candidates WHERE id = ? AND status = 'shelved'", (cid,)):
                await self.db.execute(
                    "UPDATE candidates SET status = 'pending', tier = 'resurfaced', updated_at = datetime('now') WHERE id = ?", (cid,)
                )
                changes.append(f"tog fram {cid} från hyllan")
        for p in out.get("posts") or []:
            if p.get("to") in ROLES and p["to"] != "manager":
                pid = await self.posts.send("manager", p["to"], p.get("kind", "note"), p["body"])
                changes.append(f"post #P{pid} till {p['to']}")
        for c in out.get("agenda_changes") or []:
            if self.agenda is None:
                break
            ref = "#" + str(c["id"]).strip().lstrip("#").upper()
            if c["action"] == "cancel" and ref.startswith("#B"):
                changes.append(await self.agenda.cancel(int(ref[2:]), "manager", c.get("reason", "")))
            elif c["action"] == "cancel" and ref.startswith("#F"):
                await self.db.execute("UPDATE candidates SET status = 'shelved', tier = 'shelf' WHERE id = ? AND status = 'scheduled'", (int(ref[2:]),))
                changes.append(f"{ref} sparad till senare i stället: {c.get('reason', '')}")
            elif c["action"] == "move" and (at := self.agenda.parse(c.get("at") or "")):
                changes.append(await self.agenda.move(ref, at, "manager", c.get("reason", "")))
        if review := out.get("desk_review"):
            changes.append(
                f"stickprov på desken: {review.get('sampled', 0)} beslut, "
                + ("följde policyn" if review.get("followed_policy") else "gled från policyn")
                + (f": {review['notes']}" if review.get("notes") else "")
            )
        if wm := (out.get("working_memory_md") or "").strip():
            path = self.memory.path / WORKING_MEMORY
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(wm + "\n")
        await self.posts.handled(self._seen_posts, f"läst av Manager {now:%Y-%m-%d}")
        self._seen_posts = []

        if lessons := self.memory.add_lessons("manager", out.get("lessons") or [], now):
            changes += [f"lärdom: {x}" for x in lessons]

        if (p := out.get("proposal")) and not await self.db.fetchone("SELECT 1 FROM proposals WHERE status IN ('pending', 'asked')"):
            await self.db.execute(
                "INSERT INTO proposals (kind, title, description, payload) VALUES (?, ?, ?, ?)",
                (p["kind"], p["title"], p["description"], "{}"),
            )
            changes.append(f"förslag: {p['title']}")

        if out.get("checkin"):
            await self.db.kv_set("manager.checkin_pending", True)

        await trail.account("\n".join([(out.get("summary") or "").strip(), *(f"- {c}" for c in changes)]))

        if note := (out.get("note_to_owner") or "").strip():
            await add_candidate(
                self.db, kind="manager_note", title="Från Manager", body=note,
                features={"time_sensitivity": "low", "relevance": "medium", "actionability": "low"},
            )

        await self.memory.commit_if_changed(f"manager {now:%Y-%m-%d %H:%M}", by="manager")
        log.info("manager applied: %s", "; ".join(changes) or "inga ändringar")

    # --- proposals (approved by användaren) --------------------------------------

    async def decide_proposal(self, proposal_id: int, accepted: bool) -> str:
        row = await self.db.fetchone("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        if row is None or row["status"] not in ("pending", "asked"):
            return "Det förslaget är redan avgjort."
        await self.db.execute(
            "UPDATE proposals SET status = ?, decided_at = datetime('now') WHERE id = ?",
            ("accepted" if accepted else "rejected", proposal_id),
        )
        await self.db.execute(
            "INSERT INTO feedback (signal, value, note) VALUES ('proposal', ?, ?)",
            ("accepted" if accepted else "rejected", row["title"]),
        )
        if not accepted:
            return "Okej, jag låter bli."
        return "Bra, jag tar med det nästa gång jag ser över inställningarna."

    async def next_question(self) -> dict | None:
        """A pending proposal or check-in for the dispatcher's question slot."""
        if await self.db.fetchone("SELECT 1 FROM proposals WHERE status = 'asked'"):
            return None
        row = await self.db.fetchone("SELECT * FROM proposals WHERE status = 'pending' ORDER BY id LIMIT 1")
        if row:
            return {"type": "proposal", "id": row["id"], "text": f"Förslag: {row['title']}\n\n{row['description']}"}
        if await self.db.kv_get("manager.checkin_pending"):
            return {"type": "checkin", "question": CHECKIN_QUESTION, "options": CHECKIN_OPTIONS}
        return None

    async def mark_asked(self, q: dict, message_row_id: int | None) -> None:
        if q["type"] == "proposal":
            await self.db.execute("UPDATE proposals SET status = 'asked', message_id = ? WHERE id = ?", (message_row_id, q["id"]))
        else:
            await self.db.kv_set("manager.checkin_pending", False)
            await self.db.kv_set(CHECKIN_KEY, self.now().isoformat())

    # --- context ----------------------------------------------------------

    async def _desk(self, since: str) -> str:
        """The dispatcher's decisions since the last run, with reasons, and the misses: what the owner wanted but didn't get."""
        rows = await self.db.fetchall(
            "SELECT d.created_at, d.tier, d.reason, c.id, c.kind, c.title FROM decisions d JOIN candidates c ON c.id = d.candidate_id "
            "WHERE d.module LIKE 'dispatcher%' AND d.created_at >= ? ORDER BY d.id", (since,),
        )
        lines = [f"Decisions ({len(rows)}):"]
        lines += [f"- {self._local(r['created_at'])} #F{r['id']} {r['kind']}: {r['title'][:70]} → {r['tier']}: {(r['reason'] or '')[:140]}" for r in rows[-40:]]
        misses = await self.db.fetchall(
            "SELECT f.created_at, f.signal, f.value, f.note, c.id, c.title, c.tier FROM feedback f LEFT JOIN candidates c ON c.id = f.candidate_id "
            "WHERE f.created_at >= ? AND (f.signal = 'shelf_pull' OR f.value LIKE 'missed%') ORDER BY f.id", (since,),
        )
        if misses:
            lines.append("Misses (the owner pulled it from what was saved, or asked why they weren't told):")
            lines += [
                f"- {self._local(m['created_at'])} {m['signal']}: " + (f"#F{m['id']} {m['title'][:70]} (the desk said {m['tier']})" if m["id"] else (m["note"] or "")[:140])
                for m in misses
            ]
        return "\n".join(lines) if rows or misses else "No decisions since the last run."

    async def _measures(self) -> str:
        """Measures of the experience, computed here and interpreted by Manager: signals, not targets."""
        sent = await self.db.fetchall(
            "SELECT id, created_at FROM messages WHERE direction = 'out' AND origin = 'proactive' AND created_at >= datetime('now', '-7 days')"
        )
        delays = []
        for m in sent:
            reply = await self.db.fetchone(
                "SELECT created_at FROM messages WHERE direction = 'in' AND id > ? AND created_at <= datetime(?, '+6 hours') ORDER BY id LIMIT 1",
                (m["id"], m["created_at"]),
            )
            if reply:
                delays.append((datetime.fromisoformat(reply["created_at"]) - datetime.fromisoformat(m["created_at"])).total_seconds() / 60)
        delays.sort()
        reactions = await self.db.fetchall(
            "SELECT c.kind, f.value, count(*) AS n FROM feedback f JOIN candidates c ON c.id = f.candidate_id "
            "WHERE f.signal = 'reaction' AND f.created_at >= datetime('now', '-7 days') GROUP BY c.kind, f.value ORDER BY n DESC LIMIT 10"
        )
        by_hour = await self.db.fetchall(
            "SELECT strftime('%H', created_at, 'localtime') AS h, count(*) AS n FROM messages WHERE direction = 'out' AND origin = 'proactive' "
            "AND created_at >= datetime('now', '-7 days') GROUP BY h ORDER BY h"
        )
        pauses = await self.db.fetchone("SELECT count(*) AS n FROM feedback WHERE signal = 'command' AND created_at >= datetime('now', '-7 days')")
        pulls = await self.db.fetchone("SELECT count(*) AS n FROM feedback WHERE signal = 'shelf_pull' AND created_at >= datetime('now', '-7 days')")
        return "\n".join([
            f"Proactive messages, last 7 days: {len(sent)} ({len(sent) / 7:.1f} per day); by hour: "
            + (", ".join(f"{r['h']}:{r['n']}" for r in by_hour) or "none"),
            f"Replied to within 6 h: {len(delays)} of {len(sent)}" + (f"; median after {delays[len(delays) // 2]:.0f} min" if delays else ""),
            "Reactions by kind: " + (", ".join(f"{r['kind']} {r['value']}×{r['n']}" for r in reactions) or "none"),
            f"Pause or focus requests: {pauses['n']}. Things pulled from what was saved: {pulls['n']}.",
            await Pursuits(self.db, self.settings.timezone).stats(7),
        ])

    async def _experiments(self) -> str:
        items = Items(self.db)
        rows = await items.find("experiment", owner="manager", where="status IN ('planned', 'running', 'evaluating') "
                                "OR updated_at >= datetime('now', '-30 days')", order="id")
        beliefs = await items.find("belief", status="current", owner="manager")
        lines = [f"- {Items.line(e)} · judge {e['review_at']}\n  {e.get('hypothesis', '')}" + (f"\n  Outcome: {e['outcome']}" if e.get("outcome") else "")
                 for e in rows]
        lines += [f"- {Items.line(b)}" for b in beliefs]
        return "\n".join(lines) or "None yet."

    async def _answers(self, last: str | None) -> str:
        since = (datetime.fromisoformat(last).astimezone(ZoneInfo("UTC")) if last else datetime.now(ZoneInfo("UTC")) - timedelta(days=1))
        found = await answered_since(self.db, "manager", since.strftime("%Y-%m-%d %H:%M:%S"))
        return "\n".join(f"- {Items.line(q)}: " + (q.get("answer") or "inget svar") for q in found) or "None."

    async def _activity(self, since: str) -> str:
        sent = await self.db.fetchall(
            "SELECT m.id, m.created_at, m.text, "
            "(SELECT group_concat(DISTINCT c.kind) FROM message_candidates mc JOIN candidates c ON c.id = mc.candidate_id WHERE mc.message_id = m.id) AS kinds, "
            "(SELECT group_concat(f.signal || ':' || f.value, ', ') FROM feedback f WHERE f.message_id = m.id) AS fb "
            "FROM messages m WHERE m.direction = 'out' AND m.origin = 'proactive' AND m.created_at >= ? ORDER BY m.id",
            (since,),
        )
        outcomes = await self.db.fetchall(
            "SELECT kind, status, count(*) AS n FROM candidates WHERE updated_at >= ? GROUP BY kind, status", (since,)
        )
        research = await self.db.fetchall(
            "SELECT research_id, sum(findings) AS f, count(*) AS runs, sum(error IS NOT NULL) AS errors "
            "FROM research_runs WHERE created_at >= datetime(?, '-7 days') GROUP BY research_id",
            (since,),
        )
        by_source = await self.db.fetchall(
            "SELECT e.source, c.status, count(*) AS n FROM candidates c JOIN events e ON e.id = c.event_id "
            "WHERE c.created_at >= datetime('now', '-14 days') GROUP BY e.source, c.status"
        )
        lines = [f"Proactive messages: {len(sent)}"]
        for r in sent:
            lines.append(f"- {self._local(r['created_at'])} [{r['kinds'] or '?'}] {r['fb'] or 'no reaction'} :: {r['text'][:140]!r}")
        lines.append("Candidates (kind/status): " + (", ".join(f"{r['kind']}/{r['status']}×{r['n']}" for r in outcomes) or "none"))
        lines.append("Search missions in the last week: " + (", ".join(f"{r['research_id']}: {r['runs']} runs, {r['f']} findings, {r['errors']} errors" for r in research) or "none"))
        lines.append("Outcome per source (14 days): " + (", ".join(f"{r['source']}/{r['status']}×{r['n']}" for r in by_source) or "none"))
        return "\n".join(lines)

    async def _feedback(self, since: str) -> str:
        rows = await self.db.fetchall("SELECT signal, value, note, created_at FROM feedback WHERE created_at >= ? ORDER BY id", (since,))
        week = await self.db.fetchall(
            "SELECT signal, value, count(*) AS n FROM feedback WHERE created_at >= datetime('now', '-7 days') GROUP BY signal, value ORDER BY n DESC"
        )
        lines = [f"- {self._local(r['created_at'])} {r['signal']}:{r['value']}" + (f" ({r['note'][:200]})" if r["note"] else "") for r in rows]
        lines.append("Last week in total: " + (", ".join(f"{r['signal']}:{r['value']}×{r['n']}" for r in week) or "none"))
        return "\n".join(lines)

    async def _conversation(self, since: str) -> str:
        """The whole conversation since the last run, as the owner saw it (for the UX review)."""
        rows = await self.db.fetchall(
            "SELECT direction, origin, text, created_at FROM messages WHERE created_at >= ? ORDER BY id", (since,)
        )
        lines = []
        for r in rows[-80:]:
            who = "owner" if r["direction"] == "in" else ("Audax (proactive)" if r["origin"] == "proactive" else "Audax")
            text = r["text"] if len(r["text"]) <= 1500 else r["text"][:1500] + "…"
            lines.append(f"{self._local(r['created_at'])} {who} [{len(r['text'])} chars]: {text}")
        return "\n".join(lines) or "Nothing."


    async def _shelf(self) -> str:
        await self.db.execute(  # mechanical hygiene: shelved items without a date go stale after three weeks
            "UPDATE candidates SET status = 'expired' WHERE status = 'shelved' AND expires_at IS NULL "
            "AND updated_at < datetime('now', '-21 days')"
        )
        rows = await self.db.fetchall(
            "SELECT id, kind, title, features, expires_at, updated_at FROM candidates WHERE status = 'shelved' ORDER BY updated_at DESC LIMIT 25"
        )
        return "\n".join(
            f"- [{r['id']}] {r['kind']} · {r['title'][:90]} · relevance {json.loads(r['features']).get('relevance', '?')}"
            + (f" · expires {r['expires_at'][:10]}" if r["expires_at"] else "")
            for r in rows
        ) or "Empty."

    async def _proposals(self) -> str:
        rows = await self.db.fetchall("SELECT title, status, decided_at FROM proposals ORDER BY id DESC LIMIT 10")
        return "\n".join(f"- {r['title']}: {r['status']}" for r in rows) or "None yet."

    async def _misc(self) -> str:
        session = await self.db.kv_get("chat.session_id")
        turns = (await self.db.fetchone("SELECT count(*) AS n FROM claude_runs WHERE kind = 'chat' AND session_id = ?", (session,)))["n"] if session else 0
        checkin = await self.db.kv_get(CHECKIN_KEY)
        runs = await self.db.fetchall(
            "SELECT kind, count(*) AS n, sum(is_error) AS e FROM claude_runs WHERE created_at >= datetime('now', '-1 day') GROUP BY kind"
        )
        return "\n".join([
            f"Chat session: {turns} turns since it started",
            f"Last check-in: {checkin[:10] if checkin else 'never'}",
            "Claude runs in the last 24 h: " + (", ".join(f"{r['kind']}×{r['n']} ({r['e']} errors)" for r in runs) or "none"),
            await Pursuits(self.db, self.settings.timezone).stats(),
            f"Quiet hours: {self.settings.quiet_hours.start:%H:%M}–{self.settings.quiet_hours.end:%H:%M}",
        ])

    def _local(self, sqlite_utc: str) -> str:
        d = datetime.fromisoformat(sqlite_utc + "+00:00").astimezone(self.tz)
        return f"{['mån', 'tis', 'ons', 'tor', 'fre', 'lör', 'sön'][d.weekday()]} {d:%H:%M}"
