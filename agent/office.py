"""The office as data (docs/MODEL.md): what each role is doing right now, what it has done, what it sent the others.

The web page (agent/site.py) shows this as plain pages; a future illustrated office reads the same data as JSON. Read
only: nothing here changes the office.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent import refs
from agent.claude_gate import RUNNING
from agent.db import Database

ROLES = ("assistant", "director", "researcher", "manager")
NAMES = {"assistant": "Assistant", "director": "Director", "researcher": "Researcher", "manager": "Manager",
         "build": "Byggläget", "owner": "ägaren", "system": "systemet", "other": "övrigt"}

# Which role a Claude run belongs to: its slow task, its fast task and its helpers.
ROLE_OF_KIND = {
    "chat": "assistant", "assistant_night": "assistant",
    "director_understand": "director", "director_reflect": "director", "triage": "director",
    "researcher_plan": "researcher", "research": "researcher", "research_deep": "researcher", "page_extract": "researcher",
    "scout_queries": "researcher",
    "manager": "manager", "dispatcher": "manager",
    "builder": "build",
}
TASKS = {
    "chat": "samtalet", "assistant_night": "nattgenomgången",
    "director_understand": "den dagliga genomgången", "director_reflect": "reflektionen", "triage": "triage",
    "researcher_plan": "planeringen", "research": "ett sökuppdrag", "research_deep": "en bred sökning",
    "page_extract": "läser en bevakad sida", "scout_queries": "sökfrågor (gammalt)",
    "manager": "dagens granskning", "dispatcher": "dispatchern", "builder": "en kodändring",
}

# Who owns which files in memory/ (the memory map in agent/prompts/claude_md.md). A trailing / is a folder.
FILES = {
    "assistant": ["arbetsminne/assistant.md", "profile.md", "interests.md", "projects.md", "school.md", "schedule.md", "interrupt.md"],
    "director": ["arbetsminne/director.md", "direction/person.md", "direction/now.md", "direction/goals.md", "direction/threads/",
                 "direction/ideas.md", "direction/log.md"],
    "researcher": ["arbetsminne/researcher.md", "research/maps/", "research/log.md"],
    "manager": ["arbetsminne/manager.md", "policy.md", "style.md", "state.md", "journal/", "changelog.md"],
}


def role_of(kind: str) -> str:
    return ROLE_OF_KIND.get(kind, "other")


def kinds_of(role: str) -> list[str]:
    return [k for k, r in ROLE_OF_KIND.items() if r == role]


class Office:
    def __init__(self, db: Database, memory: Path, tz: str, status=None):
        self.db = db
        self.memory = memory
        self.tz = ZoneInfo(tz)
        self.status = status  # the gate's status() (quota), when running inside the engine

    def local(self, utc: str | float | None) -> str | None:
        """A UTC time from the database (or a timestamp) as local time, to the minute."""
        if utc is None or utc == "":
            return None
        if isinstance(utc, float | int):
            d = datetime.fromtimestamp(utc, self.tz)
        else:
            d = datetime.fromisoformat(utc.replace("Z", "+00:00"))
            d = (d if d.tzinfo else d.replace(tzinfo=ZoneInfo("UTC"))).astimezone(self.tz)
        return f"{d:%Y-%m-%d %H:%M}"

    # --- right now --------------------------------------------------------------

    async def now(self) -> dict:
        roles: dict[str, dict] = {r: {"name": NAMES[r], "working": []} for r in ROLES}
        for task, started in list(RUNNING.values()):
            role = role_of(task.kind)
            roles.setdefault(role, {"name": NAMES.get(role, role), "working": []})["working"].append({
                "kind": task.kind,
                "task": TASKS.get(task.kind, task.kind),
                "since": self.local(started),
                "seconds": int(time.time() - started),
                "steps": [f"{t['tool']}: {t['input']}" for t in task.trace[-6:]],
            })
        for role in ROLES:
            kinds = kinds_of(role)
            last = await self.db.fetchone(
                f"SELECT id, kind, created_at, is_error FROM claude_runs WHERE kind IN ({','.join('?' * len(kinds))}) ORDER BY id DESC LIMIT 1", tuple(kinds)
            )
            roles[role]["last_run"] = None if last is None else {
                "id": last["id"], "task": TASKS.get(last["kind"], last["kind"]), "at": self.local(last["created_at"]), "error": bool(last["is_error"])
            }
            unread = await self.db.fetchone(
                "SELECT count(*) AS n FROM posts WHERE recipient = ? AND status = 'new' AND NOT (kind = 'question' AND recipient = 'assistant')", (role,)
            )
            roles[role]["inbox"] = unread["n"]
        to_ask = await self.db.fetchone("SELECT count(*) AS n FROM posts WHERE recipient = 'assistant' AND kind = 'question' AND status IN ('new', 'asked')")
        roles["assistant"]["questions_for_owner"] = to_ask["n"]
        night = await self.db.kv_get("night.state")
        return {
            "at": self.local(time.time()),
            "roles": roles,
            "night": night,
            "quota": self.status() if self.status else None,
        }

    # --- what happened ------------------------------------------------------------

    async def timeline(self, limit: int = 60, role: str | None = None) -> list[dict]:
        """Messages with the owner, posts between the roles and finished runs, newest first."""
        events: list[dict] = []
        if role in (None, "assistant"):
            for m in await self.db.fetchall(
                "SELECT m.id, m.created_at, m.direction, m.origin, m.text, (SELECT group_concat(ref, ' ') FROM message_refs r WHERE r.message_id = m.id) AS refs "
                "FROM messages m ORDER BY m.id DESC LIMIT ?", (limit,),
            ):
                inbound = m["direction"] == "in"
                events.append({
                    "type": "message", "utc": m["created_at"], "at": self.local(m["created_at"]),
                    "from": "owner" if inbound else "assistant", "to": "assistant" if inbound else "owner",
                    "origin": m["origin"], "text": m["text"], "refs": (m["refs"] or "").split(),
                })
        where, args = ("WHERE sender = ? OR recipient = ?", (role, role)) if role else ("", ())
        for p in await self.db.fetchall(f"SELECT * FROM posts {where} ORDER BY id DESC LIMIT ?", (*args, limit)):
            events.append({
                "type": "post", "utc": p["created_at"], "at": self.local(p["created_at"]), "ref": f"#P{p['id']}",
                "from": p["sender"], "to": p["recipient"], "kind": p["kind"], "status": p["status"], "text": p["body"],
                "verbatim": p["verbatim"],
            })
        kinds = kinds_of(role) if role else None
        where, args = (f"WHERE kind IN ({','.join('?' * len(kinds))})", tuple(kinds)) if kinds else ("", ())
        for r in await self.db.fetchall(
            f"SELECT id, kind, created_at, duration_ms, cost_usd, is_error, error FROM claude_runs {where} ORDER BY id DESC LIMIT ?", (*args, limit)
        ):
            events.append({
                "type": "run", "utc": r["created_at"], "at": self.local(r["created_at"]), "id": r["id"], "role": role_of(r["kind"]),
                "kind": r["kind"], "task": TASKS.get(r["kind"], r["kind"]), "seconds": (r["duration_ms"] or 0) // 1000,
                "cost_usd": r["cost_usd"], "error": r["error"] if r["is_error"] else None,
            })
        events.sort(key=lambda e: e["utc"], reverse=True)
        return events[:limit]

    async def run(self, run_id: int) -> dict | None:
        r = await self.db.fetchone("SELECT * FROM claude_runs WHERE id = ?", (run_id,))
        if r is None:
            return None
        out = dict(r)
        out["at"] = self.local(r["created_at"])
        out["role"] = role_of(r["kind"])
        out["task"] = TASKS.get(r["kind"], r["kind"])
        out["trace"] = json.loads(r["trace"]) if r["trace"] else []
        out["rate_limit"] = json.loads(r["rate_limit"]) if r["rate_limit"] else None
        return out

    # --- a role's desk ------------------------------------------------------------

    async def role(self, role: str) -> dict:
        inbox = await self.db.fetchall(
            "SELECT * FROM posts WHERE recipient = ? AND status IN ('new', 'asked') ORDER BY id", (role,)
        )
        return {
            "name": NAMES[role],
            "working_memory": self.read(f"arbetsminne/{role}.md"),
            "files": [self._entry(f) for f in FILES[role]],
            "inbox": [{"ref": f"#P{p['id']}", "at": self.local(p["created_at"]), "from": p["sender"], "kind": p["kind"],
                       "status": p["status"], "text": p["body"]} for p in inbox],
            "timeline": await self.timeline(80, role),
        }

    def read(self, rel: str) -> str | None:
        path = self.memory / rel
        return path.read_text() if path.is_file() else None

    def _entry(self, rel: str) -> dict:
        path = self.memory / rel
        if rel.endswith("/"):
            files = sorted(p.relative_to(self.memory).as_posix() for p in path.glob("*.md")) if path.is_dir() else []
            return {"path": rel, "folder": True, "files": files}
        return {"path": rel, "folder": False, "exists": path.is_file()}

    # --- following a case by its id ------------------------------------------------

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
                messages.append({"at": self.local(msg["created_at"]), "from": "owner" if msg["direction"] == "in" else "assistant", "text": msg["text"]})
        posts = await self.db.fetchall(
            "SELECT * FROM posts WHERE body LIKE ? OR context LIKE ? OR outcome LIKE ? OR (? = 'P' AND (answer_to = ? OR id = ?)) ORDER BY id",
            (f"%{ref}%", f"%{ref}%", f"%{ref}%", kind, n, n),
        )
        bookings = await self.db.fetchall("SELECT id, kind, status, title, due_at FROM bookings WHERE ref = ? ORDER BY id", (ref,))
        decisions = []
        if kind == "F":
            decisions = [
                {"at": self.local(d["created_at"]), "module": d["module"], "tier": d["tier"], "reason": d["reason"]}
                for d in await self.db.fetchall("SELECT created_at, module, tier, reason FROM decisions WHERE candidate_id = ? ORDER BY id", (n,))
            ]
        return {
            "ref": ref,
            "about": about,
            "messages": messages,
            "posts": [{"ref": f"#P{p['id']}", "at": self.local(p["created_at"]), "from": p["sender"], "to": p["recipient"],
                       "kind": p["kind"], "status": p["status"], "text": p["body"]} for p in posts if f"#P{p['id']}" != ref],
            "bookings": [{"ref": f"#B{b['id']}", "kind": b["kind"], "status": b["status"], "title": b["title"], "due": self.local(b["due_at"])}
                         for b in bookings],
            "decisions": decisions,
        }
