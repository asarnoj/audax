"""The illustrated office's live feed (web/kontor.html, docs/KONTORET.md): what waits where, and what just happened.

Read only, from what the office already records: new events from the senses (the printer), triage and dispatcher
decisions, posts between the roles, messages, bookings, what was taken from the board, memory saves, and the
runs in progress. The page opens on the state of right now and then follows the stream; there is no replay.

The page's own words are English; what the roles write is Swedish, because the owner writes Swedish.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from agent.claude_gate import RUNNING
from agent.db import Database
from agent.office import FILES, NAMES, ROLES, role_of

ABOUT = {
    "assistant": "Talks with you: the only one who does, and the one who remembers what you have said.",
    "director": "Works out what matters to you, and why. Goes through everything new that comes in (triage).",
    "researcher": "Finds what is out there and what the search is missing: the search missions, the maps, the watched sources.",
    "manager": "Decides when and how something reaches you, and keeps the schedule.",
}
TASK = {
    "chat": "Conversation", "assistant_night": "Night review",
    "director_understand": "Daily review", "director_reflect": "Reflection", "triage": "Triage",
    "researcher_plan": "Planning", "research": "Search mission", "research_deep": "Wide search", "page_extract": "Reading a watched page",
    "scout_queries": "Search queries", "manager": "Daily review", "dispatcher": "Dispatcher", "builder": "Code change",
}
KINDS = {r: [k for k in TASK if role_of(k) == r] for r in ROLES}
OUT_TOOLS = ("mail_", "web_read", "WebSearch", "github")   # going out of the office: Director borrows Researcher's computer
DRAWERS = ("assistant", "manager", "director", "researcher")  # the archive's drawers, left to right
SAVE_ROLE = {"chat": "assistant", "assistant": "assistant", "director": "director", "manager": "manager", "researcher": "researcher"}


class Live:
    def __init__(self, db: Database, memory: Path, tz: str):
        self.db = db
        self.memory = memory
        self.tz = ZoneInfo(tz)

    def _local(self, utc: str | None) -> str | None:
        if not utc:
            return None
        d = datetime.fromisoformat(utc.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=ZoneInfo("UTC"))).astimezone(self.tz).isoformat(timespec="minutes")

    # --- right now ------------------------------------------------------------------------

    def working(self) -> dict:
        out = {}
        for task, started in list(RUNNING.values()):
            role = role_of(task.kind)
            if role in ROLES:
                out[role] = {"kind": task.kind, "task": TASK.get(task.kind, task.kind), "seconds": int(time.time() - started),
                             "out": any(t["tool"].startswith(OUT_TOOLS) for t in task.trace)}
        return out

    async def _count(self, sql: str, args: tuple = ()) -> int:
        return (await self.db.fetchone(sql, args))[0]

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
        return [{"ref": f"#F{r['id']}", "title": r["title"], "url": r["url"], "at": self._local(r["updated_at"])} for r in rows]

    async def calendar(self, days: int = 14) -> list[dict]:
        until = (datetime.now(ZoneInfo("UTC")) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        items = []
        for r in await self.db.fetchall("SELECT id, title, deliver_at FROM candidates WHERE status = 'scheduled' AND deliver_at <= ? ORDER BY deliver_at", (until,)):
            items.append({"ref": f"#F{r['id']}", "kind": "finding", "title": r["title"], "at": self._local(r["deliver_at"])})
        for b in await self.db.fetchall("SELECT id, kind, title, due_at, fixed FROM bookings WHERE status = 'planned' AND due_at <= ? ORDER BY due_at", (until,)):
            items.append({"ref": f"#B{b['id']}", "kind": "reminder" if b["fixed"] else b["kind"], "title": b["title"], "at": self._local(b["due_at"])})
        return sorted(items, key=lambda i: i["at"] or "")

    async def binned_today(self) -> int:
        start = datetime.now(self.tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
        return await self._count("SELECT count(*) FROM decisions d WHERE d.created_at >= ? AND (d.tier = 'drop' OR d.tier = 'släppt')",
                                 (start.strftime("%Y-%m-%d %H:%M:%S"),))

    async def last_active(self) -> dict:
        """When each role last finished some work."""
        out = {}
        for role in ROLES:
            row = await self.db.fetchone(f"SELECT max(created_at) AS t FROM claude_runs WHERE kind IN ({','.join('?' * len(KINDS[role]))})", tuple(KINDS[role]))
            out[role] = self._local(row["t"]) if row and row["t"] else None
        return out

    async def state(self) -> dict:
        return {
            "working": self.working(),
            "last": await self.last_active(),
            "waiting": await self.waiting(),
            "board": await self.board(),
            "calendar": await self.calendar(),
            "bin": await self.binned_today(),
        }

    # --- what just happened ------------------------------------------------------------------

    async def _max(self, table: str) -> int:
        return (await self.db.fetchone(f"SELECT coalesce(max(id), 0) FROM {table}"))[0]

    async def _head(self) -> str:
        proc = await asyncio.create_subprocess_exec("git", "-C", str(self.memory), "rev-parse", "HEAD",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        return out.decode().strip()

    async def cursor(self) -> dict:
        c = {t: await self._max(t) for t in ("events", "candidates", "decisions", "posts", "messages", "bookings", "feedback")}
        c["git"] = await self._head()
        c["running"] = {str(k): [task.kind, any(t["tool"].startswith(OUT_TOOLS) for t in task.trace)] for k, (task, _) in list(RUNNING.items())}
        c["sent"] = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
        return c

    async def changes(self, c: dict) -> list[dict]:
        """What happened since the cursor (which it moves on), as a list of happenings for the page to act out."""
        out: list[dict] = []

        # Runs starting and ending; a Director run going out of the office (mail, web, GitHub).
        now = {str(k): (task.kind, any(t["tool"].startswith(OUT_TOOLS) for t in task.trace)) for k, (task, _) in list(RUNNING.items())}
        for k, (kind, going_out) in now.items():
            before = c["running"].get(k)
            if before is None:
                out.append({"type": "start", "role": role_of(kind), "kind": kind})
            if going_out and not (before and before[1]):
                out.append({"type": "out", "role": role_of(kind), "kind": kind})
        for k, (kind, _) in c["running"].items():
            if k not in now:
                out.append({"type": "end", "role": role_of(kind), "kind": kind})
        c["running"] = {k: list(v) for k, v in now.items()}

        # The printer: new things from the senses.
        rows = await self.db.fetchall("SELECT id FROM events WHERE id > ? ORDER BY id", (c["events"],))
        if rows:
            out.append({"type": "print", "n": len(rows)})
            c["events"] = rows[-1]["id"]

        # Triage: new findings either go on to Manager or into the bin.
        rows = await self.db.fetchall("SELECT id, status, tier FROM candidates WHERE id > ? ORDER BY id", (c["candidates"],))
        dropped = sum(1 for r in rows if r["status"] == "dropped" and r["tier"] == "drop")
        if dropped:
            out.append({"type": "drop", "n": dropped})
        for r in rows:
            if not (r["status"] == "dropped" and r["tier"] == "drop"):
                out.append({"type": "pass", "ref": f"#F{r['id']}"})
        if rows:
            c["candidates"] = rows[-1]["id"]

        # The dispatcher: where Manager puts each finding.
        rows = await self.db.fetchall("SELECT id, candidate_id, module, tier FROM decisions WHERE id > ? ORDER BY id", (c["decisions"],))
        for r in rows:
            if r["module"].startswith("dispatcher"):
                label = r["tier"] or ""
                to = ("calendar" if label.startswith(("planerad", "väntar")) else "board" if label.startswith("sparad")
                      else "assistant" if label == "nu" else "bin" if label == "släppt" else None)
                if to:
                    out.append({"type": "place", "to": to, "ref": f"#F{r['candidate_id']}"})
        if rows:
            c["decisions"] = rows[-1]["id"]

        # Posts between the roles.
        rows = await self.db.fetchall("SELECT id, sender, recipient, kind FROM posts WHERE id > ? ORDER BY id", (c["posts"],))
        for r in rows:
            out.append({"type": "post", "from": r["sender"], "to": r["recipient"], "kind": r["kind"], "ref": f"#P{r['id']}"})
        if rows:
            c["posts"] = rows[-1]["id"]

        # Messages: the owner writing, the Assistant answering, and deliveries (from the calendar or the inbox).
        rows = await self.db.fetchall("SELECT id, direction, origin FROM messages WHERE id > ? ORDER BY id", (c["messages"],))
        for r in rows:
            if r["direction"] == "in":
                out.append({"type": "message", "dir": "in"})
                continue
            source = None
            if r["origin"] == "proactive":
                linked = await self.db.fetchone(
                    "SELECT c.tier FROM message_candidates mc JOIN candidates c ON c.id = mc.candidate_id WHERE mc.message_id = ? LIMIT 1", (r["id"],))
                booked = await self.db.fetchone("SELECT 1 FROM bookings WHERE message_id = ?", (r["id"],))
                source = "calendar" if booked or (linked and linked["tier"] == "at") else "inbox" if linked else None
            out.append({"type": "message", "dir": "out", "from": source})
        if rows:
            c["messages"] = rows[-1]["id"]

        # Bookings put on the calendar.
        rows = await self.db.fetchall("SELECT id, requested_by FROM bookings WHERE id > ? ORDER BY id", (c["bookings"],))
        for r in rows:
            out.append({"type": "book", "by": r["requested_by"] if r["requested_by"] in ROLES else "assistant", "ref": f"#B{r['id']}"})
        if rows:
            c["bookings"] = rows[-1]["id"]

        # Saved findings taken from the board when the owner asked.
        rows = await self.db.fetchall("SELECT id FROM feedback WHERE id > ? AND signal = 'shelf_pull' ORDER BY id", (c["feedback"],))
        if rows:
            out.append({"type": "pull", "n": len(rows)})
        last = await self._max("feedback")
        c["feedback"] = max(c["feedback"], last)

        # Memory saves: a trip to the role's own drawer in the archive.
        head = await self._head()
        if head and head != c["git"]:
            proc = await asyncio.create_subprocess_exec("git", "-C", str(self.memory), "log", "--format=@%s", "--name-only", f"{c['git']}..{head}",
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            text, _ = await proc.communicate()
            for block in text.decode().split("@")[1:]:
                lines = [ln for ln in block.splitlines() if ln.strip()]
                role = SAVE_ROLE.get(lines[0].split()[0] if lines else "", None)
                if role:
                    out.append({"type": "archive", "role": role, "files": lines[1:6]})
            c["git"] = head
        return out

    # --- what a click shows ------------------------------------------------------------------

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
            return {"title": "Archive", "about": "The memory: one drawer per role. Everyone reads everything; each role writes only in its own drawer.",
                    "drawers": drawers}
        if name == "bin":
            rows = await self.db.fetchall(
                "SELECT c.id, c.title, d.module, d.reason, d.created_at FROM decisions d JOIN candidates c ON c.id = d.candidate_id "
                "WHERE d.tier = 'drop' OR d.tier = 'släppt' ORDER BY d.id DESC LIMIT 25")
            return {"title": "Waste bin", "about": f"What was sorted away. {await self.binned_today()} today.",
                    "items": [{"ref": f"#F{r['id']}", "title": r["title"], "at": self._local(r["created_at"]),
                               "by": "Director (triage)" if r["module"].startswith("triage") else "Manager", "why": r["reason"]} for r in rows]}
        if name == "calendar":
            until = (datetime.now(self.tz) + timedelta(days=7)).isoformat()
            own = await self.db.fetchall(
                "SELECT summary, start, end, all_day FROM calendar_events WHERE status = 'active' AND start >= ? AND start <= ? ORDER BY start LIMIT 40",
                (datetime.now(self.tz).isoformat(), until))
            return {"title": "Calendar", "about": "What the office plans to bring up with you, and your own sessions it plans around.",
                    "planned": await self.calendar(), "yours": [{"title": r["summary"], "start": r["start"], "end": r["end"]} for r in own]}
        if name == "board":
            return {"title": "Board", "about": "Findings worth keeping but not worth interrupting you for. The Assistant takes from here when you ask.",
                    "items": await self.board()}
        return None

    async def inbox(self, role: str) -> list[dict]:
        """What lies in a role's inbox, oldest first."""
        items = []
        for p in await self.db.fetchall(
            "SELECT id, sender, kind, body, created_at FROM posts WHERE recipient = ? AND status IN ('new', 'asked') ORDER BY id", (role,)):
            items.append({"ref": f"#P{p['id']}", "what": p["kind"], "from": NAMES.get(p["sender"], p["sender"]), "text": p["body"], "at": self._local(p["created_at"])})
        if role == "director":
            for e in await self.db.fetchall("SELECT id, source, payload, created_at FROM events WHERE triaged_at IS NULL ORDER BY id LIMIT 40"):
                try:
                    payload = json.loads(e["payload"])
                except ValueError:
                    payload = {}
                items.append({"what": "new", "from": e["source"].split(":", 1)[-1], "text": payload.get("title") or payload.get("summary") or e["source"],
                              "at": self._local(e["created_at"])})
        if role == "manager":
            for c in await self.db.fetchall("SELECT id, title, created_at FROM candidates WHERE status = 'pending' ORDER BY id"):
                items.append({"ref": f"#F{c['id']}", "what": "finding", "from": "Director", "text": c["title"], "at": self._local(c["created_at"])})
        return items

    async def _desk(self, role: str) -> dict:
        wm = self.memory / "arbetsminne" / f"{role}.md"
        runs = await self.db.fetchall(
            f"SELECT id, kind, created_at, duration_ms, is_error FROM claude_runs WHERE kind IN ({','.join('?' * len(KINDS[role]))}) ORDER BY id DESC LIMIT 60",
            tuple(KINDS[role]))
        return {
            "role": role, "title": NAMES[role], "about": ABOUT[role],
            "working": self.working().get(role),
            "last": (await self.last_active())[role],
            "inbox": await self.inbox(role),
            "memory": wm.read_text() if wm.is_file() else None,
            "work": [{"id": r["id"], "task": TASK.get(r["kind"], r["kind"]), "at": self._local(r["created_at"]),
                      "seconds": (r["duration_ms"] or 0) // 1000, "failed": bool(r["is_error"])} for r in runs],
        }

    async def _git(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec("git", "-C", str(self.memory), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        return out.decode(errors="replace")

    async def work(self, run_id: int) -> dict | None:
        """One piece of work: what it was about, how it went about it, what it wrote, and what it changed."""
        r = await self.db.fetchone("SELECT * FROM claude_runs WHERE id = ?", (run_id,))
        if r is None:
            return None
        role = role_of(r["kind"])
        end = datetime.fromisoformat(r["created_at"]).replace(tzinfo=ZoneInfo("UTC"))
        start = end - timedelta(milliseconds=r["duration_ms"] or 0)
        sql = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
        window = (sql(start - timedelta(seconds=5)), sql(end + timedelta(seconds=60)))
        about: list[dict] = []
        if r["kind"] == "chat":
            m = await self.db.fetchone("SELECT text FROM messages WHERE direction = 'in' AND created_at <= ? ORDER BY id DESC LIMIT 1", (sql(end),))
            if m:
                about.append({"label": "You wrote", "text": m["text"]})
        elif r["kind"] == "triage":
            for c in await self.db.fetchall("SELECT id, title, status, tier FROM candidates WHERE created_at BETWEEN ? AND ? ORDER BY id", window):
                why = await self.db.fetchone("SELECT reason FROM decisions WHERE candidate_id = ? ORDER BY id LIMIT 1", (c["id"],))
                kept = not (c["status"] == "dropped" and c["tier"] == "drop")
                about.append({"label": "Kept" if kept else "Thrown away", "text": c["title"], "ref": f"#F{c['id']}", "note": why["reason"] if why and not kept else None})
        elif r["kind"] == "dispatcher":
            for d in await self.db.fetchall(
                "SELECT d.tier, d.reason, c.id, c.title FROM decisions d JOIN candidates c ON c.id = d.candidate_id "
                "WHERE d.module LIKE 'dispatcher%' AND d.created_at BETWEEN ? AND ? ORDER BY d.id", window):
                about.append({"label": d["tier"], "text": d["title"], "ref": f"#F{d['id']}", "note": d["reason"]})
        elif r["kind"] in ("research", "research_deep"):
            for e in await self.db.fetchall("SELECT payload FROM events WHERE source LIKE 'research%' AND created_at BETWEEN ? AND ? ORDER BY id", window):
                try:
                    about.append({"label": "Found", "text": json.loads(e["payload"]).get("title", "")})
                except ValueError:
                    pass
        posts = await self.db.fetchall(
            "SELECT id, recipient, kind, body FROM posts WHERE sender = ? AND created_at BETWEEN ? AND ? ORDER BY id", (role, *window))
        edits = []
        log = await self._git("log", "--format=%H %ct %s", f"--since={int(start.timestamp()) - 5}", f"--until={int(end.timestamp()) + 60}")
        for line in log.splitlines():
            sha, _, msg = line.split(" ", 2)
            if SAVE_ROLE.get(msg.split()[0] if msg else "") != role:
                continue
            patch = await self._git("show", "--format=", "--unified=1", "--no-color", sha)
            edits.append({"message": msg, "patch": patch[:12000] + ("\n…" if len(patch) > 12000 else "")})
        return {
            "id": r["id"], "task": TASK.get(r["kind"], r["kind"]), "role": role, "at": self._local(r["created_at"]),
            "seconds": (r["duration_ms"] or 0) // 1000, "failed": bool(r["is_error"]), "error": r["error"],
            "about": about[:40],
            "tools": [{"tool": t["tool"], "input": t["input"], "failed": t.get("error", False)} for t in (json.loads(r["trace"]) if r["trace"] else [])],
            "wrote": r["output"],
            "posts": [{"ref": f"#P{p['id']}", "to": NAMES.get(p["recipient"], p["recipient"]), "kind": p["kind"], "text": p["body"]} for p in posts],
            "edits": edits,
        }


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
