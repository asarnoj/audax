"""The internal mail (docs/MODEL.md, "Postgången"): how the four roles talk to each other.

One central log; each role's inbox is the part of it addressed to that role. A post is a note, a question, an order,
feedback or an answer, with the owner's exact words when they matter. Roles never wake each other directly: they post,
and the engine wakes. Waking is only allowed for chains that began with the owner (the owner said something), so the
office can't loop; urgent findings travel forward to the owner through the pipeline instead. Only the Assistant talks
to the owner: questions for the owner are posts to the Assistant, asked one at a time.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from agent import trail
from agent.db import Database

log = logging.getLogger(__name__)

ROLES = ("assistant", "director", "researcher", "manager")
KINDS = ("note", "question", "answer", "order", "feedback", "report")
NAMES = {"assistant": "Assistant", "director": "Director", "researcher": "Researcher", "manager": "Manager", "system": "systemet"}


def ref(post_id: int) -> str:
    return f"#P{post_id}"


class Posts:
    waker: Callable[[str], None] | None = None  # set by main: starts a role's run soon (coalesced, rate-limited)

    def __init__(self, db: Database):
        self.db = db

    async def send(
        self, sender: str, to: str, kind: str, body: str, *, verbatim: str | None = None, context: str | None = None,
        answer_to: int | None = None, from_owner: bool = False, wake: bool = False,
    ) -> int:
        assert to in ROLES and kind in KINDS, (to, kind)
        pid = await self.db.insert("posts", sender=sender, recipient=to, kind=kind, body=body, verbatim=verbatim, context=context,
                                   answer_to=answer_to, from_owner=int(from_owner))
        await trail.emit("post", sender, to=to, what=kind, ref=ref(pid))
        if answer_to is not None:
            await self.db.execute(
                "UPDATE posts SET status = 'done', handled_at = datetime('now'), outcome = ? WHERE id = ?", (f"besvarad i {ref(pid)}", answer_to)
            )
        if wake:
            if from_owner and Posts.waker is not None:
                Posts.waker(to)  # only chains that began with the owner may wake a role
            else:
                log.info("post %s asked to wake %s, but only the owner's words may wake a role; it waits", pid, to)
        return pid

    # --- a role's inbox ------------------------------------------------------

    async def inbox(self, role: str) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT p.*, q.body AS question FROM posts p LEFT JOIN posts q ON q.id = p.answer_to "
            "WHERE p.recipient = ? AND p.status = 'new' AND NOT (p.kind = 'question' AND p.recipient = 'assistant') ORDER BY p.id", (role,),
        )
        return [dict(r) for r in rows]

    async def handled(self, ids: list[int], outcome: str | None = None) -> None:
        for pid in ids:
            await self.db.execute(
                "UPDATE posts SET status = 'done', handled_at = datetime('now'), outcome = COALESCE(?, outcome) WHERE id = ? AND status = 'new'",
                (outcome, pid),
            )

    @staticmethod
    def format(posts: list[dict]) -> str:
        """For a role's prompt: who wrote, the owner's exact words when there are any, and the sender's reading."""
        lines = []
        for p in posts:
            head = f"- {ref(p['id'])} from {NAMES.get(p['sender'], p['sender'])} ({p['kind']})"
            if p.get("question"):
                head += f", answering \"{p['question']}\""
            if p.get("verbatim"):
                lines.append(f"{head}: the owner's words \"{p['verbatim']}\" → {p['body']}")
            else:
                lines.append(f"{head}: {p['body']}")
        return "\n".join(lines) or "Nothing new."

    # --- questions for the owner (asked by the Assistant, one at a time) ---------

    async def open_question(self) -> dict | None:
        """The oldest question waiting to be asked, unless one is already waiting for an answer."""
        mine = "kind = 'question' AND recipient = 'assistant'"
        if await self.db.fetchone(f"SELECT 1 FROM posts WHERE {mine} AND status = 'asked' AND asked_at >= datetime('now', '-2 days')"):
            return None
        await self.db.execute(f"UPDATE posts SET status = 'expired' WHERE {mine} AND status = 'asked' AND asked_at < datetime('now', '-2 days')")
        row = await self.db.fetchone(f"SELECT * FROM posts WHERE {mine} AND status = 'new' ORDER BY id LIMIT 1")
        return dict(row) if row else None

    async def asked(self, post_id: int, message_id: int | None = None) -> None:
        await self.db.execute(
            "UPDATE posts SET status = 'asked', asked_at = datetime('now'), message_id = COALESCE(?, message_id) WHERE id = ?",
            (message_id, post_id),
        )

    async def get(self, post_id: int) -> dict | None:
        row = await self.db.fetchone("SELECT * FROM posts WHERE id = ?", (post_id,))
        return dict(row) if row else None
