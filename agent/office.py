"""The office's data, for its page (web/office.html, docs/KONTORET.md) and the raw-data pages: read only.

- the state of right now: who is working, what waits in each inbox, the board, the calendar, the bin;
- what happened since a point in the office's event log (agent/trail.py), for the page to act out;
- what a click shows: a desk (its inbox, its work, its notes), the archive, the bin, the calendar, the board;
- one piece of work (#R): what it was about, its answer, what it sent and changed, its steps, all found by the
  run's id, never guessed from times;
- a case by its id (#P, #S, #F, #B), for the raw-data pages.

The page's words are English; what the roles write is Swedish, because the owner writes Swedish.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from agent import refs
from agent.claude_gate import RUNNING
from agent.db import Database
from agent.items import Items
from agent.vocab import KINDS, NAMES, OUT_TOOLS, ROLES, kinds_of, role_of, task_name

UTC = ZoneInfo("UTC")
ABOUT = {
    "assistant": "Talks with you: the only one who does, and the one who remembers what you have said.",
    "director": "Works out what matters to you, and why. Goes through everything new that comes in (triage).",
    "researcher": "Finds what is out there and what the search is missing: the search missions, the maps, the watched sources.",
    "manager": "Decides when and how something reaches you, and keeps the schedule.",
}
# Who owns which notes in the memory (the memory map in agent/prompts/claude_md.md); a trailing / is a folder.
FILES = {
    "assistant": ["arbetsminne/assistant.md", "profile.md"],
    "director": ["arbetsminne/director.md", "direction/person.md", "direction/now.md"],
    "researcher": ["arbetsminne/researcher.md", "research/maps/"],
    "manager": ["arbetsminne/manager.md", "policy.md"],
}
DRAWERS = ("assistant", "manager", "director", "researcher")  # the archive's drawers, left to right
# The tools, in words a person reads.
TOOL = {
    "web_read": "Read a web page", "WebSearch": "Searched the web", "WebFetch": "Read a web page", "Read": "Read a note", "Edit": "Edited a note",
    "Write": "Wrote a note", "send_post": "Sent a post", "get_shelf": "Looked at the board", "shelf_told": "Took from the board",
    "lookup": "Looked something up", "book": "Booked a time", "change_booking": "Moved a booking", "get_agenda": "Looked at the calendar",
    "get_schedule": "Looked at your schedule", "mail_search": "Searched your mail", "mail_read": "Read a mail", "github": "Looked at GitHub",
    "track_pursuit": "Noted what you're going for", "record_feedback": "Noted your feedback", "watch_source": "Started watching a source",
    "change_watch": "Changed a watched source", "mark_question_asked": "Asked you a question", "how_i_work": "Looked up how the office works",
    "run_research": "Started a search", "report_problem": "Reported a problem", "pause_messages": "Paused messages", "interview_guide": "Opened the interview guide",
    "search_overview": "Looked at how a search is done", "source_overview": "Looked at the watched sources", "explain_message": "Looked up why a message was sent",
    "activity_report": "Looked at today's activity", "request_change": "Asked for a change to Audax",
}
RUN_TAG = re.compile(r"\[#R(\d+)\]")


def decision(label: str) -> str:
    """The dispatcher's decision, in words."""
    if label.startswith("planerad"):
        return "To the calendar, " + label.removeprefix("planerad ").strip()
    if label.startswith("väntar"):
        return "Moved on the calendar, " + label.removeprefix("väntar till ").strip()
    return {"nu": "Sent to you now", "sparad till senare": "Saved to the board", "släppt": "Dropped", "drop": "Thrown away"}.get(label, label)


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class Office:
    def __init__(self, db: Database, memory: Path, tz: str, status=None, budget=None):
        self.db = db
        self.memory = memory
        self.tz = ZoneInfo(tz)
        self.status = status  # the gate's status() (the quota), when running inside the engine
        self.budget = budget  # agent/budget.Budget, when running inside the engine

    def local(self, utc: str | None) -> str | None:
        if not utc:
            return None
        d = datetime.fromisoformat(utc.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(self.tz).isoformat(timespec="minutes")

    async def _count(self, sql: str, args: tuple = ()) -> int:
        return (await self.db.fetchone(sql, args))[0]

    async def _git(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec("git", "-C", str(self.memory), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        return out.decode(errors="replace")

    # --- right now ----------------------------------------------------------------------------

    def working(self) -> dict:
        out = {}
        for task, started in list(RUNNING.values()):
            role = role_of(task.kind)
            if role in ROLES:
                out[role] = {"kind": task.kind, "task": task_name(task.kind), "seconds": int(time.time() - started),
                             "out": any(t["tool"].startswith(OUT_TOOLS) for t in task.trace)}
        return out

    async def last_active(self) -> dict:
        out = {}
        for role in ROLES:
            kinds = kinds_of(role)
            row = await self.db.fetchone(f"SELECT max(finished_at) AS t FROM claude_runs WHERE kind IN ({','.join('?' * len(kinds))})", tuple(kinds))
            out[role] = self.local(row["t"]) if row and row["t"] else None
        return out

    async def waiting(self) -> dict:
        """How much lies in each role's inbox: everything waiting for it, posts included."""
        posts = {r: await self._count("SELECT count(*) FROM posts WHERE recipient = ? AND status = 'new' AND NOT (kind = 'question' AND recipient = 'assistant')", (r,))
                 for r in ROLES}
        return {
            "director": posts["director"] + await self._count("SELECT count(*) FROM events WHERE triaged_at IS NULL"),
            "manager": posts["manager"] + await self._count("SELECT count(*) FROM candidates WHERE status = 'pending'"),
            "assistant": posts["assistant"] + await self._count(
                "SELECT count(*) FROM posts WHERE recipient = 'assistant' AND kind = 'question' AND status IN ('new', 'asked')"),
            "researcher": posts["researcher"],
        }

    async def board(self, limit: int = 30) -> list[dict]:
        rows = await self.db.fetchall("SELECT id, title, url, updated_at FROM candidates WHERE status = 'shelved' ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [{"ref": f"#F{r['id']}", "title": r["title"], "url": r["url"], "at": self.local(r["updated_at"])} for r in rows]

    async def calendar(self, days: int = 35) -> list[dict]:
        until = (datetime.now(UTC) + timedelta(days=days)).isoformat()
        items = []
        for r in await self.db.fetchall("SELECT id, title, deliver_at FROM candidates WHERE status = 'scheduled' AND deliver_at <= ? ORDER BY deliver_at", (until,)):
            items.append({"ref": f"#F{r['id']}", "kind": "finding", "title": r["title"], "at": self.local(r["deliver_at"])})
        for b in await self.db.fetchall("SELECT id, kind, title, due_at, fixed FROM bookings WHERE status = 'planned' AND due_at <= ? ORDER BY due_at", (until,)):
            items.append({"ref": f"#B{b['id']}", "kind": "reminder" if b["fixed"] else b["kind"], "title": b["title"], "at": self.local(b["due_at"])})
        return sorted(items, key=lambda i: i["at"] or "")

    async def binned_today(self) -> int:
        start = datetime.now(self.tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        return await self._count("SELECT count(*) FROM decisions WHERE created_at >= ? AND tier IN ('drop', 'släppt')", (start.strftime("%Y-%m-%d %H:%M:%S"),))

    async def counts(self) -> dict:
        """What the piles show: sent with the state, and after every batch of happenings."""
        return {"waiting": await self.waiting(), "board": len(await self.board()), "calendar": await self.calendar(), "bin": await self.binned_today()}

    async def cursor(self) -> int:
        return (await self.db.fetchone("SELECT coalesce(max(id), 0) FROM office_events"))[0]

    async def state(self) -> dict:
        return {"cursor": await self.cursor(), "working": self.working(), "last": await self.last_active(), "boardItems": await self.board(),
                **await self.counts(), "names": {r: NAMES[r] for r in ROLES}, "tasks": {k: name for k, (_, name) in KINDS.items()}}

    # --- what happened ------------------------------------------------------------------------

    async def since(self, after: int, limit: int = 200) -> list[dict]:
        """The office's events after a point in its log (agent/trail.py), oldest first."""
        rows = await self.db.fetchall("SELECT id, kind, role, data, run_id FROM office_events WHERE id > ? ORDER BY id LIMIT ?", (after, limit))
        return [{"id": r["id"], "type": r["kind"], "role": r["role"], "run": r["run_id"], **json.loads(r["data"] or "{}")} for r in rows]

    # --- what a click shows ---------------------------------------------------------------------

    async def detail(self, name: str) -> dict | None:
        if name.startswith("desk-") and name[5:] in ROLES:
            return await self._desk(name[5:])
        if name == "archive":
            drawers = []
            for role in DRAWERS:
                files = []
                for rel in FILES[role]:
                    path = self.memory / rel
                    for p in (sorted(path.glob("*.md")) if rel.endswith("/") else [path]):
                        if p.is_file():
                            files.append({"path": p.relative_to(self.memory).as_posix(),
                                          "changed": datetime.fromtimestamp(p.stat().st_mtime, self.tz).isoformat(timespec="minutes")})
                drawers.append({"role": role, "name": NAMES[role], "files": files})
            goals = await Items(self.db).find(
                "aim", open_only=True, order="kind != 'goal', kind != 'project', coalesce(json_extract(data, '$.deadline'), '9999'), id")
            return {"title": "Archive", "about": "The memory: one drawer per role. Everyone reads everything; each role writes only in its own drawer.",
                    "drawers": drawers,
                    "records": [{"ref": f"#S{g['id']}", "kind": g["kind"], "title": g["title"], "status": g["status"], "deadline": g.get("deadline")} for g in goals]}
        if name == "bin":
            rows = await self.db.fetchall(
                "SELECT c.id, c.title, d.module, d.reason, d.created_at FROM decisions d JOIN candidates c ON c.id = d.candidate_id "
                "WHERE d.tier IN ('drop', 'släppt') ORDER BY d.id DESC LIMIT 25")
            return {"title": "Waste bin", "about": f"What was sorted away. {await self.binned_today()} today.",
                    "items": [{"ref": f"#F{r['id']}", "title": r["title"], "at": self.local(r["created_at"]),
                               "by": "Director (triage)" if r["module"].startswith("triage") else "Manager", "why": r["reason"]} for r in rows]}
        if name == "calendar":
            until = (datetime.now(self.tz) + timedelta(days=7)).isoformat()
            own = await self.db.fetchall(
                "SELECT summary, start FROM calendar_events WHERE status = 'active' AND start >= ? AND start <= ? ORDER BY start LIMIT 40",
                (datetime.now(self.tz).isoformat(), until))
            return {"title": "Calendar", "about": "What the office plans to bring up with you, and your own sessions it plans around.",
                    "planned": await self.calendar(), "yours": [{"title": r["summary"], "start": r["start"]} for r in own]}
        if name == "board":
            return {"title": "Board", "about": "Findings worth keeping but not worth interrupting you for. The Assistant takes from here when you ask.",
                    "items": await self.board()}
        return None

    async def inbox(self, role: str) -> list[dict]:
        """What lies in a role's inbox, oldest first."""
        items = []
        for p in await self.db.fetchall(
            "SELECT id, sender, kind, body, created_at FROM posts WHERE recipient = ? AND status IN ('new', 'asked') ORDER BY id", (role,)):
            items.append({"ref": f"#P{p['id']}", "what": p["kind"], "from": NAMES.get(p["sender"], p["sender"]), "text": p["body"], "at": self.local(p["created_at"])})
        if role == "director":
            for e in await self.db.fetchall("SELECT source, payload, created_at FROM events WHERE triaged_at IS NULL ORDER BY id LIMIT 40"):
                payload = json.loads(e["payload"] or "{}")
                items.append({"what": "new", "from": e["source"].split(":", 1)[-1], "text": payload.get("title") or payload.get("summary") or e["source"],
                              "at": self.local(e["created_at"])})
        if role == "manager":
            for c in await self.db.fetchall("SELECT id, title, created_at FROM candidates WHERE status = 'pending' ORDER BY id"):
                items.append({"ref": f"#F{c['id']}", "what": "finding", "from": "Director", "text": c["title"], "at": self.local(c["created_at"])})
        return items

    async def _desk(self, role: str) -> dict:
        wm = self.memory / "arbetsminne" / f"{role}.md"
        kinds = kinds_of(role)
        runs = await self.db.fetchall(
            f"SELECT id, kind, created_at, finished_at, duration_ms, is_error FROM claude_runs WHERE kind IN ({','.join('?' * len(kinds))}) "
            "AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 60", tuple(kinds))
        summary = await self._summaries(runs)
        return {
            "role": role, "title": NAMES[role], "about": ABOUT[role],
            "working": self.working().get(role), "last": (await self.last_active())[role],
            "inbox": await self.inbox(role),
            "memory": wm.read_text() if wm.is_file() else None,
            "work": [{"id": r["id"], "kind": r["kind"], "task": task_name(r["kind"]), "summary": summary.get(r["id"]),
                      "at": self.local(r["finished_at"]), "seconds": (r["duration_ms"] or 0) // 1000, "failed": bool(r["is_error"])} for r in runs],
        }

    async def _summaries(self, runs: list) -> dict[int, str]:
        """One line per piece of work saying what happened, from what the run wrote (by its id)."""
        if not runs:
            return {}
        ids = [r["id"] for r in runs]
        marks = ",".join("?" * len(ids))
        by_run = lambda rows: {r["run_id"]: r for r in rows}  # noqa: E731
        posts = by_run(await self.db.fetchall(f"SELECT run_id, count(*) AS n FROM posts WHERE run_id IN ({marks}) GROUP BY run_id", tuple(ids)))
        cands = by_run(await self.db.fetchall(
            f"SELECT run_id, count(*) AS n, sum(status = 'dropped' AND tier = 'drop') AS dropped FROM candidates WHERE run_id IN ({marks}) GROUP BY run_id", tuple(ids)))
        found = by_run(await self.db.fetchall(
            f"SELECT run_id, count(*) AS n FROM events WHERE run_id IN ({marks}) AND source LIKE 'research%' GROUP BY run_id", tuple(ids)))
        decs: dict[int, list[str]] = {}
        for d in await self.db.fetchall(f"SELECT run_id, tier FROM decisions WHERE run_id IN ({marks}) AND module LIKE 'dispatcher%'", tuple(ids)):
            decs.setdefault(d["run_id"], []).append(decision(d["tier"]).split(",")[0])
        oldest = min(r["created_at"] for r in runs)
        saved = {int(m) for m in RUN_TAG.findall(await self._git("log", "--format=%s", f"--since={oldest}Z"))}
        out = {}
        for r in runs:
            rid, k = r["id"], r["kind"]
            extra = ([f"sent {plural(posts[rid]['n'], 'post')}"] if rid in posts else []) + (["changed its notes"] if rid in saved else [])
            if k == "chat":
                m = await self.db.fetchone("SELECT text FROM messages WHERE direction = 'in' AND created_at <= ? ORDER BY id DESC LIMIT 1", (r["created_at"],))
                line = f"Answered you: “{m['text'][:70]}{'…' if len(m['text']) > 70 else ''}”" if m else "Wrote to you"
            elif k == "triage" and rid in cands:
                c = cands[rid]
                line = f"Went through {plural(c['n'], 'new item')} · kept {c['n'] - (c['dropped'] or 0)}"
            elif k == "dispatcher" and rid in decs:
                tally: dict[str, int] = {}
                for d in decs[rid]:
                    tally[d] = tally.get(d, 0) + 1
                line = f"Decided on {plural(len(decs[rid]), 'finding')}: " + ", ".join(f"{n} {d.lower()}" for d, n in tally.items())
            elif k in ("research", "research_deep"):
                line = f"{task_name(k)} · found {found[rid]['n'] if rid in found else 0}"
            else:
                line = task_name(k)
            out[rid] = " · ".join([line, *extra]) + (" · failed" if r["is_error"] else "")
        return out

    async def work(self, run_id: int) -> dict | None:
        """One piece of work: what it was about, its answer, what it sent and changed, and its steps."""
        r = await self.db.fetchone("SELECT * FROM claude_runs WHERE id = ?", (run_id,))
        if r is None:
            return None
        about: list[dict] = []
        if r["kind"] == "chat":
            m = await self.db.fetchone("SELECT text FROM messages WHERE direction = 'in' AND created_at <= ? ORDER BY id DESC LIMIT 1", (r["created_at"],))
            if m:
                about.append({"label": "You wrote", "text": m["text"]})
        for c in await self.db.fetchall("SELECT id, title, status, tier FROM candidates WHERE run_id = ? ORDER BY id", (run_id,)):
            kept = not (c["status"] == "dropped" and c["tier"] == "drop")
            why = None if kept else await self.db.fetchone("SELECT reason FROM decisions WHERE candidate_id = ? ORDER BY id LIMIT 1", (c["id"],))
            about.append({"label": "Kept" if kept else "Thrown away", "text": c["title"], "ref": f"#F{c['id']}", "note": why["reason"] if why else None})
        for d in await self.db.fetchall(
            "SELECT d.tier, d.reason, c.id, c.title FROM decisions d JOIN candidates c ON c.id = d.candidate_id "
            "WHERE d.run_id = ? AND d.module LIKE 'dispatcher%' ORDER BY d.id", (run_id,)):
            about.append({"label": decision(d["tier"]), "text": d["title"], "ref": f"#F{d['id']}", "note": d["reason"]})
        for e in await self.db.fetchall("SELECT payload FROM events WHERE run_id = ? AND source LIKE 'research%' ORDER BY id", (run_id,)):
            about.append({"label": "Found", "text": json.loads(e["payload"] or "{}").get("title", "")})
        sent = await self.db.fetchall("SELECT text FROM messages WHERE run_id = ? AND direction = 'out' ORDER BY id", (run_id,))
        answer = "\n\n".join(m["text"] for m in sent) or (None if (r["output"] or "").lstrip().startswith("{") else r["output"])
        posts = await self.db.fetchall("SELECT id, recipient, kind, body FROM posts WHERE run_id = ? ORDER BY id", (run_id,))
        edits = []
        for sha in (await self._git("log", "--all", "-F", f"--grep=[#R{run_id}]", "--format=%H")).split():
            patch = await self._git("show", "--format=", "--unified=1", "--no-color", sha)
            for part in patch.split("diff --git ")[1:]:   # one entry per note it changed
                lines = part.splitlines()
                name = next((ln[6:] for ln in lines if ln.startswith("+++ b/")), lines[0].split(" b/")[-1] if lines else "?")
                body = "\n".join(ln for ln in lines if ln[:1] in "+- " and not ln.startswith(("+++", "---")))
                edits.append({"file": name, "added": sum(ln.startswith("+") for ln in body.splitlines()),
                              "removed": sum(ln.startswith("-") for ln in body.splitlines()), "patch": body[:8000] + ("\n…" if len(body) > 8000 else "")})
        return {
            "id": r["id"], "task": task_name(r["kind"]), "role": role_of(r["kind"]), "at": self.local(r["finished_at"] or r["created_at"]),
            "seconds": (r["duration_ms"] or 0) // 1000, "failed": bool(r["is_error"]), "error": r["error"],
            "account": r["summary"], "about": about[:40], "answer": answer,
            "posts": [{"ref": f"#P{p['id']}", "to": NAMES.get(p["recipient"], p["recipient"]), "kind": p["kind"], "text": p["body"]} for p in posts],
            "edits": edits,
            "tools": [{"tool": TOOL.get(t["tool"].removeprefix("agent__"), t["tool"].removeprefix("agent__")), "input": t["input"], "failed": t.get("error", False)}
                      for t in (json.loads(r["trace"]) if r["trace"] else []) if t["tool"] != "StructuredOutput"],
        }

    # --- raw data: one run, one case ----------------------------------------------------------

    async def run(self, run_id: int) -> dict | None:
        r = await self.db.fetchone("SELECT * FROM claude_runs WHERE id = ?", (run_id,))
        if r is None:
            return None
        out = dict(r)
        out.update(at=self.local(r["created_at"]), role=role_of(r["kind"]), task=task_name(r["kind"]),
                   trace=json.loads(r["trace"]) if r["trace"] else [], rate_limit=json.loads(r["rate_limit"]) if r["rate_limit"] else None)
        return out

    async def case(self, ref: str) -> dict | None:
        m = refs.PATTERN.fullmatch(ref.strip())
        if not m:
            return None
        kind, n = m.group(1).upper(), int(m.group(2))
        ref = f"#{kind}{n}"
        about = await refs.describe(self.db, ref)
        if about is None:
            return None
        message_ids = {r["message_id"] for r in await self.db.fetchall("SELECT message_id FROM message_refs WHERE ref = ?", (ref,))}
        if kind == "F":
            message_ids |= {r["id"] for r in await self.db.fetchall("SELECT id FROM messages WHERE candidate_id = ?", (n,))}
            message_ids |= {r["message_id"] for r in await self.db.fetchall("SELECT message_id FROM message_candidates WHERE candidate_id = ?", (n,))}
        messages = []
        for mid in sorted(message_ids):
            msg = await self.db.fetchone("SELECT * FROM messages WHERE id = ?", (mid,))
            if msg:
                messages.append({"at": self.local(msg["created_at"]), "from": "you" if msg["direction"] == "in" else "Assistant", "text": msg["text"]})
        posts = await self.db.fetchall(
            "SELECT * FROM posts WHERE body LIKE ? OR context LIKE ? OR outcome LIKE ? OR (? = 'P' AND (answer_to = ? OR id = ?)) ORDER BY id",
            (f"%{ref}%", f"%{ref}%", f"%{ref}%", kind, n, n),
        )
        bookings = await self.db.fetchall("SELECT id, kind, status, title, due_at FROM bookings WHERE ref = ? ORDER BY id", (ref,))
        decisions = []
        if kind == "F":
            decisions = [{"at": self.local(d["created_at"]), "module": d["module"], "decision": decision(d["tier"]), "reason": d["reason"]}
                         for d in await self.db.fetchall("SELECT created_at, module, tier, reason FROM decisions WHERE candidate_id = ? ORDER BY id", (n,))]
        return {
            "ref": ref, "about": about, "messages": messages, "decisions": decisions,
            "posts": [{"ref": f"#P{p['id']}", "at": self.local(p["created_at"]), "from": NAMES.get(p["sender"], p["sender"]),
                       "to": NAMES.get(p["recipient"], p["recipient"]), "kind": p["kind"], "status": p["status"], "text": p["body"]}
                      for p in posts if f"#P{p['id']}" != ref],
            "bookings": [{"ref": f"#B{b['id']}", "kind": b["kind"], "status": b["status"], "title": b["title"], "due": self.local(b["due_at"])}
                         for b in bookings],
        }


def sse(event: dict) -> str:
    """One server-sent event; its id lets a reconnecting page carry on from where it was."""
    head = f"id: {event['id']}\n" if "id" in event else ""
    return head + f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
