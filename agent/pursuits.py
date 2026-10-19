"""What the owner goes for (docs/MODEL.md, "ID:n och uppslag"): one kind of record, #S, instead of free-text files.

Four kinds, each with a title, why, status, next step, deadline and free notes (`about`, Swedish):
- **goal** (mål): what the owner wants and why, what helps and what is noise. Triage, Researcher and Director read
  them for everything they judge. What Director drives over time is the goal's plan, in its notes and next step.
- **project** (projekt): something the owner builds or does.
- **pursuit** (tillfälle): an opportunity they have shown interest in, applied to or turned down, under the goal it
  serves. Audax is good at finding these; what the owner asked for just as much is help past hesitation:
  - a nudge at the right time: something not yet applied to becomes, near its deadline (or on the day they asked
    to be reminded), an outbox candidate. The dispatcher decides when it goes out and the chat writes it. One
    nudge per pursuit; no nagging.
  - follow-up after applying: Director keeps statuses true from the conversation and the mail.
- **idea** (idé): Director's ideas between reflections, with whether they are being looked into, were shared or
  were dismissed (and why), under the goal they serve.

Who writes: the chat (what the owner says, and reactions), Director (everything, above all goals and ideas), and
Python (a nudge was queued; a deadline passed without applying). Every change is logged per record.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from agent.db import Database
from agent.items import Items
from agent.pipeline.outbox import add_candidate
from agent.prompts import personalize
from agent.schedule import utc_iso

log = logging.getLogger(__name__)

KINDS = ("goal", "project", "pursuit", "idea")
PURSUIT = "pursuit"   # an opportunity: the one kind of aim that gets nudges
KIND_LABEL = {"goal": "Mål", "project": "Projekt", "pursuit": "Tillfälle", "idea": "Idé"}
STATUSES = {
    "goal": ["active", "paused", "done", "dropped"],
    "project": ["active", "paused", "done", "dropped"],
    "pursuit": ["interested", "preparing", "applied", "interviewing", "offer", "accepted", "rejected", "declined", "missed"],
    "idea": ["exploring", "shared", "dismissed"],
}
OPEN = ("active", "paused", "interested", "preparing", "applied", "interviewing", "offer", "exploring", "shared")
NOT_YET_APPLIED = ("interested", "preparing")
LABEL = {
    "active": "aktivt", "paused": "pausat", "done": "klart", "dropped": "släppt",
    "interested": "intresserad", "preparing": "förbereder", "applied": "sökt", "interviewing": "intervju", "offer": "erbjudande",
    "accepted": "tackat ja", "rejected": "fick nej", "declined": "valde bort", "missed": "missade deadline",
    "exploring": "undersöks", "shared": "delad", "dismissed": "avfärdad",
}
# Mechanical fallback when nobody set a reminder day: a nudge becomes a candidate this long before the deadline.
# When it actually goes out is the dispatcher's judgement under Manager's policy.
DEFAULT_LEAD = timedelta(days=3)
POSITIVE_REACTIONS = {"👍", "❤", "❤️", "🔥", "🤩", "👏", "💯", "😍", "🥰", "🎉", "⚡", "🙏", "❤‍🔥"}
PURSUABLE = ("opportunity", "event", "insight", "search_update")
DATA = ("organization", "url", "deadline", "remind_at", "next_step", "why", "about", "candidate_id")   # the aim's own fields


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
    """The aims (#S) on top of the office's items (agent/items.py): matching, nudges and the overview the roles read."""

    def __init__(self, db: Database, tz: str = "Europe/Stockholm"):
        self.db = db
        self.items = Items(db)
        self.tz = ZoneInfo(tz)

    def today(self) -> date:
        return datetime.now(self.tz).date()

    # --- writing ----------------------------------------------------------

    async def upsert(self, fields: dict, by: str) -> tuple[int, bool]:
        """Updates the aim given by id, by candidate, or by a matching title among open ones; else adds one."""
        fields = {k: v for k, v in fields.items() if v not in (None, "")}
        if "parent" in fields:
            fields["parent_id"] = fields.pop("parent")
        for k in ("deadline", "remind_at"):
            if k in fields:
                fields[k] = _day(fields[k])
        row = await self.items.get(fields["id"]) if fields.get("id") else None
        if row is not None and row["form"] != "aim":
            raise ValueError(f"#{fields['id']} is not an aim")
        kind = row["kind"] if row else fields.get("kind") if fields.get("kind") in KINDS else "pursuit"
        if fields.get("status") not in STATUSES[kind]:
            fields.pop("status", None)
        if fields.get("parent_id") == fields.get("id"):
            fields.pop("parent_id", None)
        if fields.get("parent_id"):
            parent = await self.items.get(fields["parent_id"])
            if not parent or parent["form"] != "aim" or parent["kind"] not in ("goal", "project"):
                fields.pop("parent_id")   # only a goal or a project holds other aims
        if row is None and fields.get("candidate_id"):
            found = await self.items.find("aim", where="json_extract(data, '$.candidate_id') = ?", args=(fields["candidate_id"],), limit=1)
            row = found[0] if found else None
        if row is None and fields.get("title"):
            for r in await self.items.find("aim", kind=kind, status=OPEN):
                if _same(r["title"], fields["title"]) and (not fields.get("organization") or not r.get("organization")
                                                           or _same(r["organization"], fields["organization"])):
                    row = r
                    break
        data = {k: fields[k] for k in DATA if k in fields}
        if row is None:
            if not fields.get("title"):
                raise ValueError("a new record needs a title")
            source = {"type": "owner" if by in ("chat", "reaction") else "inferred", "by": by}
            pid = await self.items.create("aim", fields["title"], _role(by), kind=kind, status=fields.get("status"), owner="director",
                                          parent=fields.get("parent_id"), data=data, source=source, note=fields.get("note") or "lades till")
            return pid, True
        if data.get("remind_at") and data["remind_at"] != row.get("remind_at"):
            data["nudged_at"] = None  # a new reminder day means a new nudge
        if fields.get("candidate_id") and row.get("candidate_id"):
            data.pop("candidate_id", None)
        await self.items.update(row["id"], _role(by), title=fields.get("title"), status=fields.get("status"),
                                parent=fields["parent_id"] if "parent_id" in fields else ..., data=data, note=fields.get("note"))
        return row["id"], False

    async def from_candidate(self, candidate_id: int, by: str, status: str = "interested") -> int | None:
        """An aim for something Audax delivered (e.g. the owner reacted with 👍 to it)."""
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

    # --- time -------------------------------------------------------------

    async def tick(self) -> int:
        """Deadlines that passed without applying become 'missed'; due reminders become nudge candidates."""
        today = self.today()
        for r in await self.items.find("aim", kind=PURSUIT, status=NOT_YET_APPLIED,
                                       where="json_extract(data, '$.deadline') < ?", args=(today.isoformat(),)):
            await self.items.update(r["id"], "system", status="missed", note="deadline passerade utan ansökan")
        queued = 0
        for r in await self.items.find("aim", kind=PURSUIT, status=NOT_YET_APPLIED,
                                       where="json_extract(data, '$.nudged_at') IS NULL AND (json_extract(data, '$.deadline') IS NOT NULL "
                                             "OR json_extract(data, '$.remind_at') IS NOT NULL)"):
            remind = date.fromisoformat(r["remind_at"]) if r.get("remind_at") else date.fromisoformat(r["deadline"]) - DEFAULT_LEAD
            if remind > today:
                continue
            days_left = (date.fromisoformat(r["deadline"]) - today).days if r.get("deadline") else None
            expires = None
            if r.get("deadline"):
                expires = utc_iso(datetime.fromisoformat(r["deadline"]).replace(hour=23, minute=59, tzinfo=self.tz))
            body = personalize(
                f"Knuff: {{owner}} visade intresse för {r['title']}" + (f" hos {r['organization']}" if r.get("organization") else "")
                + f" men har inte sökt än ({LABEL[r['status']]})."
                + (f" Sista dag {r['deadline']}, om {days_left} dag(ar)." if days_left is not None else "")
                + (f" Nästa steg: {r['next_step']}." if r.get("next_step") else "")
                + (" {owner} bad om påminnelsen." if r.get("remind_at") else "")
            )
            cid = await add_candidate(
                self.db, kind="nudge", title=r["title"], body=body, url=r.get("url"),
                features={"relevance": "high", "actionability": "high", "time_sensitivity": "high" if days_left is not None and days_left <= 2 else "medium",
                          "novelty": "medium", "confidence": "high", "complement": "high",
                          "reason": "Knuff före deadline för något du visat intresse för"},
                expires_at=expires,
            )
            await self.items.update(r["id"], "system", data={"nudged_at": datetime.now(ZoneInfo("UTC")).replace(tzinfo=None).isoformat(timespec="minutes"),
                                                             "nudge_candidate_id": cid}, note="knuff köad")
            queued += 1
        if queued:
            log.info("pursuits: %d nudge(s) queued", queued)
        return queued

    # --- reading ----------------------------------------------------------

    async def overview(self, notes: bool = True, ideas: bool = True, closed_days: int = 30) -> str:
        """Everything open, grouped under its goal or project, then what closed lately (and every dismissed idea, so
        nothing is thought through twice). With notes: each record's why, next step and notes too (for the roles that
        judge by them); without: one line each."""
        marks = ",".join("?" * len(OPEN))
        rows = await self.items.find(
            "aim", where=f"(status IN ({marks}) OR updated_at >= datetime('now', ?) OR kind = 'idea')", args=(*OPEN, f"-{closed_days} days"),
            order="kind != 'goal', kind != 'project', COALESCE(json_extract(data, '$.deadline'), '9999'), id",
        )
        rows = [r for r in rows if ideas or r["kind"] != "idea"]
        is_open = {r["id"] for r in rows if r["status"] in OPEN}

        def line(r) -> str:
            return (
                f"#S{r['id']} {KIND_LABEL[r['kind']]}: {r['title']}" + (f" ({r['organization']})" if r.get("organization") else "")
                + f" · {LABEL.get(r['status'], r['status'])}"
                + (f" · sista dag {r['deadline']}" if r.get("deadline") else "")
                + (f" · påminn {r['remind_at']}" if r.get("remind_at") else "")
                + (" · knuffad" if r.get("nudged_at") else "")
                + (f" · {r['url']}" if r.get("url") else "")
            )

        def block(r, indent: str) -> list[str]:
            out = [indent + line(r)]
            if notes:
                out += [" " * len(indent) + "  " + text for text in (
                    (f"Varför: {r['why']}" if r.get("why") else ""), (f"Nästa: {r['next_step']}" if r.get("next_step") else ""),
                    *(r.get("about") or "").strip().splitlines()) if text.strip()]
            elif r.get("next_step"):
                out[0] += f" · nästa: {r['next_step']}"
            return out

        lines: list[str] = []
        heads = [r for r in rows if r["kind"] in ("goal", "project") and r["id"] in is_open]
        held = {r["id"] for r in heads}
        for h in heads:
            lines += block(h, "")
            for c in rows:
                if c["parent_id"] == h["id"] and c["id"] in is_open:
                    lines += block(c, "  - ")
            lines.append("")
        loose = [r for r in rows if r["id"] in is_open and r["id"] not in held and r["parent_id"] not in held]
        if loose:
            lines += ["Utan mål:"] + [x for r in loose for x in block(r, "- ")] + [""]
        closed = [r for r in rows if r["id"] not in is_open]
        if closed:
            lines += [f"Avslutat (senaste {closed_days} dagarna; idéer alltid):"]
            lines += [x for r in closed for x in (block(r, "- ") if r["kind"] == "idea" else ["- " + line(r)])]
        return "\n".join(lines).strip() or "(nothing yet)"

    async def stats(self, days: int = 30) -> str:
        """How pursuits and nudges turn out: for Manager and Director (learning, not a target)."""
        since = f"-{days} days"
        by_status = await self.db.fetchall(
            "SELECT status, count(*) AS n FROM items WHERE form = 'aim' AND kind = 'pursuit' AND updated_at >= datetime('now', ?) GROUP BY status", (since,))
        nudged = await self.db.fetchone(
            "SELECT count(*) AS n FROM items WHERE form = 'aim' AND json_extract(data, '$.nudged_at') >= date('now', ?)", (since,))
        acted = await self.db.fetchone(
            "SELECT count(DISTINCT i.id) AS n FROM items i JOIN item_log l ON l.item_id = i.id WHERE i.form = 'aim' "
            "AND json_extract(i.data, '$.nudged_at') >= date('now', ?) AND l.created_at >= datetime(json_extract(i.data, '$.nudged_at')) "
            "AND l.status IN ('preparing', 'applied')", (since,),
        )
        return (
            f"Pursuits, last {days} days: " + (", ".join(f"{LABEL.get(r['status'], r['status'])} {r['n']}" for r in by_status) or "none")
            + f". Nudges: {nudged['n']}, followed by preparing or applying: {acted['n']}."
        )


def _role(by: str) -> str:
    """Who changed it, as a role: the chat and a reaction are the Assistant recording the owner's words."""
    return "assistant" if by in ("chat", "reaction") else by


async def judged_against(db: Database, memory, tz: str) -> str:
    """What a finding or a search is judged against: the goals (what helps, what is noise) and what serves them, the
    situation and the profile. For triage and Researcher."""
    return ("--- the goals, and what the owner goes for (#S) ---\n" + await Pursuits(db, tz).overview(ideas=False)
            + "\n\n" + memory.read("direction/now.md", "profile.md"))


async def candidates_of_message(db: Database, message_row_id: int | None) -> list[int]:
    """Deliveries written by the chat link their candidates in message_candidates; older ones in messages.candidate_id."""
    if message_row_id is None:
        return []
    rows = await db.fetchall(
        "SELECT candidate_id FROM message_candidates WHERE message_id = ? UNION "
        "SELECT candidate_id FROM messages WHERE id = ? AND candidate_id IS NOT NULL", (message_row_id, message_row_id),
    )
    return [r["candidate_id"] for r in rows]
