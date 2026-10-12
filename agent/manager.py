"""Manager: owner of the experience (docs/ARCHITECTURE.md, docs/ATTENTION.md §4.6).

Nightly: reviews the conversation like a UX researcher, calibrates pacing (policy.md), voice
(style.md) and the owner's current bandwidth (state.md), and tends the shelf. What the agent looks for
is Director's job, not Manager's.

Python gathers the facts, Claude judges, Python applies the result within fixed bounds:
- policy.md, style.md, state.md, journal and changelog are written here (the chat can't edit them)
- structural changes to the experience become proposals that the owner approves with a button
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


from agent.pursuits import Pursuits
from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import Settings
from agent.db import Database
from agent.inbox import Inbox
from agent.memory import MemoryRepo
from agent.pipeline.outbox import add_candidate
from agent.prompts import prompt as prompt_template

log = logging.getLogger(__name__)

MODULE = "manager@1"
LAST_RUN_KEY = "manager.last_run"
CHECKIN_KEY = "manager.last_checkin"
CHAT_SEED_KEY = "chat.seed_summary"
CHAT_CONTEXT_LIMIT = 60_000  # tokens; above this the nightly pass starts the chat over from a summary
AREAS = ["pacing", "timing", "content", "tone"]

SCHEMA = {
    "type": "object",
    "properties": {
        "policy_md": {"type": ["string", "null"], "description": "Swedish. The whole new policy.md, or null if unchanged"},
        "policy_reason": {"type": "string", "description": "Swedish, one sentence"},
        "style_md": {"type": ["string", "null"], "description": "Swedish. The whole new style.md, or null. Only for a recurring pattern or the owner's own request."},
        "style_reason": {"type": ["string", "null"], "description": "Swedish. The evidence for the style change"},
        "state_md": {"type": ["string", "null"], "description": "Swedish. The whole new state.md, or null if the situation is unchanged"},
        "resurface": {"type": "array", "items": {"type": "integer"}, "description": "Candidate ids from the shelf to bring back"},
        "questions": {
            "type": "array",
            "items": {"type": "object", "properties": {"what": {"type": "string"}, "why": {"type": "string"}}, "required": ["what", "why"]},
            "description": "At most one question about the experience, in Swedish; the chat asks it when it fits",
        },
        "experiments_new": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "area": {"type": "string", "enum": AREAS},
                    "hypothesis": {"type": "string", "description": "Swedish: observation, change, expectation"},
                    "evaluate_after": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["area", "hypothesis", "evaluate_after"],
            },
        },
        "experiments_close": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["kept", "adjusted", "reverted"]},
                    "outcome": {"type": "string"},
                },
                "required": ["id", "status", "outcome"],
            },
        },
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
        "journal_md": {"type": "string", "description": "Swedish. Today's journal entry"},
        "changelog": {"type": "string", "description": "Swedish. What you changed and why; empty string if nothing"},
        "note_to_owner": {"type": ["string", "null"], "description": "Swedish brief for the chat, or null"},
        "restart_chat": {"type": "boolean", "description": "Only if the conversation itself needs a fresh start"},
        "chat_summary": {"type": "string", "description": "Always. Swedish: short summary of the conversation so far (ongoing threads, promises, what is current). Used if the session restarts."},
    },
    "required": ["policy_md", "policy_reason", "journal_md", "changelog", "checkin", "restart_chat", "chat_summary"],
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

    def now(self) -> datetime:
        return datetime.now(self.tz)

    # --- run ----------------------------------------------------------------

    async def run(self) -> dict | None:
        now = self.now()
        last = await self.db.kv_get(LAST_RUN_KEY)
        since = datetime.fromisoformat(last) if last else now - timedelta(days=1)
        since_sql = since.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")

        prompt = prompt_template("manager").format(
            now=f"{now:%Y-%m-%d %H:%M} ({['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][now.weekday()]})",
            since=f"{since:%Y-%m-%d %H:%M}",
            policy=self.memory.read("policy.md"),
            experiments=await self._experiments(),
            activity=await self._activity(since_sql),
            feedback=await self._feedback(since_sql),
            conversation=await self._conversation(since_sql),
            style_state=self.memory.read("style.md", "state.md"),
            notes=self._format_notes(await Inbox(self.db).unprocessed("manager")),
            shelf=await self._shelf(),
            proposals=await self._proposals(),
            misc=await self._misc(),
        )
        try:
            result = await self.gate.run(
                ClaudeTask(
                    kind="manager",
                    prompt=prompt,
                    priority=Priority.MANAGER,
                    model=self.settings.models.manager,
                    allowed_tools=["Read(./memory/**)"],  # Manager's own files are written by Python from its answer
                    output_schema=SCHEMA,
                    max_turns=25,
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
        if out.get("style_md"):
            (self.memory.path / "style.md").write_text(out["style_md"].strip() + "\n")
            changes.append(f"stilguide: {out.get('style_reason') or ''}")
        if out.get("state_md"):
            (self.memory.path / "state.md").write_text(out["state_md"].strip() + "\n")
            changes.append("läge uppdaterat")
        for cid in out.get("resurface") or []:
            if await self.db.fetchone("SELECT 1 FROM candidates WHERE id = ? AND status = 'shelved'", (cid,)):
                await self.db.execute(
                    "UPDATE candidates SET status = 'pending', tier = 'resurfaced', updated_at = datetime('now') WHERE id = ?", (cid,)
                )
                changes.append(f"tog fram {cid} från hyllan")
        for q in (out.get("questions") or [])[:1]:
            await Inbox(self.db).request("manager", q["what"], q["why"])
            changes.append(f"fråga till användaren: {q['what']}")
        await Inbox(self.db).mark_processed("manager")

        for e in out.get("experiments_close") or []:
            await self.db.execute(
                "UPDATE experiments SET status = ?, outcome = ?, closed_at = datetime('now') WHERE id = ? AND status = 'active'",
                (e["status"], e["outcome"], e["id"]),
            )
            changes.append(f"experiment {e['id']} {e['status']}: {e['outcome']}")
        active_areas = {r["area"] for r in await self.db.fetchall("SELECT area FROM experiments WHERE status = 'active'")}
        for e in out.get("experiments_new") or []:
            if e["area"] in active_areas:
                log.info("manager: skipped second experiment in area %s", e["area"])
                continue  # one active experiment per area (ATTENTION.md §4.6)
            eid = await self.db.execute(
                "INSERT INTO experiments (area, hypothesis, evaluate_after) VALUES (?, ?, ?)",
                (e["area"], e["hypothesis"], e["evaluate_after"]),
            )
            active_areas.add(e["area"])
            changes.append(f"nytt experiment H-{eid:03d} ({e['area']})")

        if (p := out.get("proposal")) and not await self.db.fetchone("SELECT 1 FROM proposals WHERE status IN ('pending', 'asked')"):
            await self.db.execute(
                "INSERT INTO proposals (kind, title, description, payload) VALUES (?, ?, ?, ?)",
                (p["kind"], p["title"], p["description"], "{}"),
            )
            changes.append(f"förslag: {p['title']}")

        if out.get("checkin"):
            await self.db.kv_set("manager.checkin_pending", True)

        journal = self.memory.path / "journal" / f"{now:%Y-%m-%d}.md"
        journal.parent.mkdir(exist_ok=True)
        existing = journal.read_text() if journal.exists() else ""
        journal.write_text(existing + (f"# {now:%Y-%m-%d}\n\n" if not existing else "\n") + out.get("journal_md", "").strip() + "\n")

        log_text = (out.get("changelog") or "").strip()
        if log_text or changes:
            with (self.memory.path / "changelog.md").open("a") as f:
                f.write(f"\n## {now:%Y-%m-%d %H:%M}\n{log_text}\n" + "".join(f"- {c}\n" for c in changes))

        if note := (out.get("note_to_owner") or "").strip():
            await add_candidate(
                self.db, kind="manager_note", title="Från Manager", body=note,
                features={"time_sensitivity": "low", "relevance": "medium", "actionability": "low"},
            )

        # Mechanical hygiene (not UX policy): a chat session that has grown big resends its whole history on
        # every reply, which is what eats the subscription quota. Start it over from the summary.
        last = await self.db.fetchone("SELECT context_tokens, cost_usd FROM claude_runs WHERE kind = 'chat' ORDER BY id DESC LIMIT 1")
        too_big = bool(last) and (
            last["context_tokens"] > CHAT_CONTEXT_LIMIT if last["context_tokens"] else (last["cost_usd"] or 0) > 1.0  # unmeasured: cost as proxy
        )
        if (out.get("restart_chat") or too_big) and out.get("chat_summary"):
            await self.db.kv_set(CHAT_SEED_KEY, out["chat_summary"])
            await self.db.kv_set("chat.session_id", None)
            changes.append("chattsessionen startades om med sammanfattning")

        await self.memory.commit_if_changed(f"manager {now:%Y-%m-%d %H:%M}")
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

    async def _experiments(self) -> str:
        rows = await self.db.fetchall("SELECT * FROM experiments WHERE status = 'active' ORDER BY id")
        return "\n\n".join(
            f"H-{r['id']:03d} ({r['area']}, since {r['created_at'][:10]}, judge from {r['evaluate_after']})\n{r['hypothesis']}"
            for r in rows
        ) or "None."

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

    @staticmethod
    def _format_notes(notes: list[dict]) -> str:
        return "\n".join(
            f"- \"{n['verbatim']}\" → {n['interpretation']}" + (f" (answer to: {n['question']})" if n.get("question") else "")
            for n in notes
        ) or "None."

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
