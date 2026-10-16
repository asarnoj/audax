"""Pursuits (docs/ATTENTION.md, "Ansökningsspåret"): what the owner is going for, so Audax can push at the right time.

Audax is good at finding opportunities; what the owner asked for just as much is help past hesitation. A pursuit is
anything they have shown interest in, applied to or turned down: with a deadline, a next step and a status. From it:

- **a nudge at the right time**: something they were interested in but haven't applied to becomes, near its deadline
  (or on the day they asked to be reminded), an outbox candidate. The dispatcher decides when it goes out and the
  chat writes it, in one sentence. One nudge per pursuit; no nagging.
- **follow-up after applying**: Director keeps statuses true from the conversation and the mail (replies, interviews).
- **learning**: what the owner pursues, lets go of and acts on after a nudge is visible to Director and Manager.

Who writes: the chat (what the owner says, and reactions), Director (conversation and mail), and Python (a nudge
was queued; a deadline passed without applying).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from agent.db import Database
from agent.pipeline.outbox import add_candidate
from agent.prompts import personalize
from agent.schedule import utc_iso

log = logging.getLogger(__name__)

STATUSES = ["interested", "preparing", "applied", "interviewing", "offer", "accepted", "rejected", "declined", "missed"]
OPEN = ("interested", "preparing", "applied", "interviewing", "offer")
NOT_YET_APPLIED = ("interested", "preparing")
LABEL = {
    "interested": "intresserad", "preparing": "förbereder", "applied": "sökt", "interviewing": "intervju", "offer": "erbjudande",
    "accepted": "tackat ja", "rejected": "fick nej", "declined": "valde bort", "missed": "missade deadline",
}
# Mechanical fallback when nobody set a reminder day: a nudge becomes a candidate this long before the deadline.
# When it actually goes out is the dispatcher's judgement under Manager's policy.
DEFAULT_LEAD = timedelta(days=3)
POSITIVE_REACTIONS = {"👍", "❤", "❤️", "🔥", "🤩", "👏", "💯", "😍", "🥰", "🎉", "⚡", "🙏", "❤‍🔥"}
PURSUABLE = ("opportunity", "event", "insight", "search_update")
FIELDS = ("title", "organization", "url", "status", "deadline", "remind_at", "next_step", "note")


def _day(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _same(a: str, b: str) -> bool:
    a, b = a.casefold().strip(), b.casefold().strip()
    return bool(a and b) and (a == b or a in b or b in a)


class Pursuits:
    def __init__(self, db: Database, tz: str = "Europe/Stockholm"):
        self.db = db
        self.tz = ZoneInfo(tz)

    def today(self) -> date:
        return datetime.now(self.tz).date()

    # --- writing ----------------------------------------------------------

    async def upsert(self, fields: dict, by: str) -> tuple[int, bool]:
        """Updates the pursuit given by id, by candidate, or by a matching title among open ones; else adds one."""
        fields = {k: v for k, v in fields.items() if v not in (None, "")}
        for k in ("deadline", "remind_at"):
            if k in fields:
                fields[k] = _day(fields[k])
        if fields.get("status") not in STATUSES:
            fields.pop("status", None)
        row = None
        if fields.get("id"):
            row = await self.db.fetchone("SELECT * FROM pursuits WHERE id = ?", (fields["id"],))
        if row is None and fields.get("candidate_id"):
            row = await self.db.fetchone("SELECT * FROM pursuits WHERE candidate_id = ?", (fields["candidate_id"],))
        if row is None and fields.get("title"):
            for r in await self.db.fetchall(f"SELECT * FROM pursuits WHERE status IN ({','.join('?' * len(OPEN))})", OPEN):
                if _same(r["title"], fields["title"]) and (not fields.get("organization") or not r["organization"] or _same(r["organization"], fields["organization"])):
                    row = r
                    break
        if row is None:
            if not fields.get("title"):
                raise ValueError("a new pursuit needs a title")
            pid = await self.db.insert("pursuits", title=fields["title"], organization=fields.get("organization"), url=fields.get("url"),
                                       candidate_id=fields.get("candidate_id"), status=fields.get("status", "interested"), deadline=fields.get("deadline"),
                                       remind_at=fields.get("remind_at"), next_step=fields.get("next_step"), note=fields.get("note"), source=by)
            await self._log(pid, fields.get("status", "interested"), fields.get("note") or "lades till", by)
            return pid, True
        changes = {k: fields[k] for k in FIELDS if k in fields and fields[k] != row[k]}
        if fields.get("candidate_id") and not row["candidate_id"]:
            changes["candidate_id"] = fields["candidate_id"]
        if changes.get("remind_at"):
            changes["nudged_at"] = None  # a new reminder day means a new nudge
        if changes:
            sets = ", ".join(f"{k} = ?" for k in changes)
            await self.db.execute(f"UPDATE pursuits SET {sets}, updated_at = datetime('now') WHERE id = ?", (*changes.values(), row["id"]))
            if "status" in changes or "note" in changes:
                await self._log(row["id"], changes.get("status"), changes.get("note"), by)
        return row["id"], False

    async def from_candidate(self, candidate_id: int, by: str, status: str = "interested") -> int | None:
        """A pursuit for something Audax delivered (e.g. the owner reacted with 👍 to it)."""
        c = await self.db.fetchone(
            "SELECT c.*, e.payload FROM candidates c LEFT JOIN events e ON e.id = c.event_id WHERE c.id = ?", (candidate_id,)
        )
        if c is None or c["kind"] not in PURSUABLE:
            return None
        payload = json.loads(c["payload"]) if c["payload"] else {}
        deadline = _day(payload.get("deadline") or payload.get("date"))
        if not deadline and c["expires_at"]:
            deadline = datetime.fromisoformat(c["expires_at"]).astimezone(self.tz).date().isoformat()
        pid, _ = await self.upsert({
            "candidate_id": candidate_id, "title": c["title"], "organization": payload.get("organization"),
            "url": c["url"], "deadline": deadline, "status": status,
        }, by)
        return pid

    async def _log(self, pid: int, status: str | None, note: str | None, by: str) -> None:
        await self.db.insert("pursuit_log", pursuit_id=pid, status=status, note=note, by=by)

    # --- time -------------------------------------------------------------

    async def tick(self) -> int:
        """Deadlines that passed without applying become 'missed'; due reminders become nudge candidates."""
        today = self.today()
        marks = ",".join("?" * len(NOT_YET_APPLIED))
        for r in await self.db.fetchall(
            f"SELECT id FROM pursuits WHERE status IN ({marks}) AND deadline IS NOT NULL AND deadline < ?", (*NOT_YET_APPLIED, today.isoformat())
        ):
            await self.db.execute("UPDATE pursuits SET status = 'missed', updated_at = datetime('now') WHERE id = ?", (r["id"],))
            await self._log(r["id"], "missed", "deadline passerade utan ansökan", "system")
        queued = 0
        for r in await self.db.fetchall(
            f"SELECT * FROM pursuits WHERE status IN ({marks}) AND nudged_at IS NULL AND (deadline IS NOT NULL OR remind_at IS NOT NULL)",
            NOT_YET_APPLIED,
        ):
            remind = date.fromisoformat(r["remind_at"]) if r["remind_at"] else date.fromisoformat(r["deadline"]) - DEFAULT_LEAD
            if remind > today:
                continue
            days_left = (date.fromisoformat(r["deadline"]) - today).days if r["deadline"] else None
            expires = None
            if r["deadline"]:
                expires = utc_iso(datetime.fromisoformat(r["deadline"]).replace(hour=23, minute=59, tzinfo=self.tz))
            body = personalize(
                f"Knuff: {{owner}} visade intresse för {r['title']}" + (f" hos {r['organization']}" if r["organization"] else "")
                + f" men har inte sökt än ({LABEL[r['status']]})."
                + (f" Sista dag {r['deadline']}, om {days_left} dag(ar)." if days_left is not None else "")
                + (f" Nästa steg: {r['next_step']}." if r["next_step"] else "")
                + (" {owner} bad om påminnelsen." if r["remind_at"] else "")
            )
            cid = await add_candidate(
                self.db, kind="nudge", title=r["title"], body=body, url=r["url"],
                features={"relevance": "high", "actionability": "high", "time_sensitivity": "high" if days_left is not None and days_left <= 2 else "medium",
                          "novelty": "medium", "confidence": "high", "complement": "high",
                          "reason": "Knuff före deadline för något du visat intresse för"},
                expires_at=expires,
            )
            await self.db.execute("UPDATE pursuits SET nudged_at = datetime('now'), nudge_candidate_id = ? WHERE id = ?", (cid, r["id"]))
            queued += 1
        if queued:
            log.info("pursuits: %d nudge(s) queued", queued)
        return queued

    # --- reading ----------------------------------------------------------

    async def summary(self, closed_days: int = 30) -> str:
        """Open pursuits, then recently closed ones: for the chat, Director and the owner."""
        rows = await self.db.fetchall(
            f"SELECT * FROM pursuits WHERE status IN ({','.join('?' * len(OPEN))}) OR updated_at >= datetime('now', ?) "
            "ORDER BY status IN ('interested', 'preparing') DESC, COALESCE(deadline, '9999') , id",
            (*OPEN, f"-{closed_days} days"),
        )
        return "\n".join(
            f"- [{r['id']}] {r['title']}" + (f" ({r['organization']})" if r["organization"] else "")
            + f" · {LABEL.get(r['status'], r['status'])}"
            + (f" · sista dag {r['deadline']}" if r["deadline"] else "")
            + (f" · påminn {r['remind_at']}" if r["remind_at"] else "")
            + (" · knuffad" if r["nudged_at"] else "")
            + (f" · nästa: {r['next_step']}" if r["next_step"] else "")
            + (f" · {r['url']}" if r["url"] else "")
            for r in rows
        ) or "(nothing yet)"

    async def stats(self, days: int = 30) -> str:
        """How pursuits and nudges turn out: for Manager and Director (learning, not a target)."""
        since = f"-{days} days"
        by_status = await self.db.fetchall("SELECT status, count(*) AS n FROM pursuits WHERE updated_at >= datetime('now', ?) GROUP BY status", (since,))
        nudged = await self.db.fetchone("SELECT count(*) AS n FROM pursuits WHERE nudged_at >= datetime('now', ?)", (since,))
        acted = await self.db.fetchone(
            "SELECT count(DISTINCT p.id) AS n FROM pursuits p JOIN pursuit_log l ON l.pursuit_id = p.id "
            "WHERE p.nudged_at >= datetime('now', ?) AND l.created_at >= p.nudged_at AND l.status IN ('preparing', 'applied')", (since,),
        )
        return (
            f"Pursuits, last {days} days: " + (", ".join(f"{LABEL.get(r['status'], r['status'])} {r['n']}" for r in by_status) or "none")
            + f". Nudges: {nudged['n']}, followed by preparing or applying: {acted['n']}."
        )


async def candidates_of_message(db: Database, message_row_id: int | None) -> list[int]:
    """Deliveries written by the chat link their candidates in message_candidates; older ones in messages.candidate_id."""
    if message_row_id is None:
        return []
    rows = await db.fetchall(
        "SELECT candidate_id FROM message_candidates WHERE message_id = ? UNION "
        "SELECT candidate_id FROM messages WHERE id = ? AND candidate_id IS NOT NULL", (message_row_id, message_row_id),
    )
    return [r["candidate_id"] for r in rows]
