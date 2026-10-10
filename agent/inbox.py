"""Requests from Director/Manager to användaren, and notes routed back to them (docs/ARCHITECTURE.md, "En röst").

Only the chat session talks to användaren. Director and Manager leave requests here; the dispatcher decides
when, and the chat asks naturally. the owner's answers and other relevant remarks come back as notes that
keep his exact words next to the chat's interpretation, so nothing is lost in translation.
"""

from __future__ import annotations

from agent.db import Database

OWNERS = ("director", "manager")


class Inbox:
    def __init__(self, db: Database):
        self.db = db

    # --- requests (Director/Manager -> användaren) --------------------------------

    async def request(self, owner: str, what: str, why: str, kind: str = "question") -> int:
        assert owner in OWNERS
        return await self.db.execute("INSERT INTO requests (owner, kind, what, why) VALUES (?, ?, ?, ?)", (owner, kind, what, why))

    async def open_request(self) -> dict | None:
        """The oldest pending request, unless one is already waiting for an answer (one question at a time)."""
        if await self.db.fetchone(
            "SELECT 1 FROM requests WHERE status = 'asked' AND asked_at >= datetime('now', '-2 days')"
        ):
            return None
        await self.db.execute(
            "UPDATE requests SET status = 'expired' WHERE status = 'asked' AND asked_at < datetime('now', '-2 days')"
        )
        row = await self.db.fetchone("SELECT * FROM requests WHERE status = 'pending' ORDER BY id LIMIT 1")
        return dict(row) if row else None

    async def mark_asked(self, request_id: int, message_id: int | None = None) -> None:
        await self.db.execute(
            "UPDATE requests SET status = 'asked', asked_at = datetime('now'), message_id = COALESCE(?, message_id) WHERE id = ?",
            (message_id, request_id),
        )

    # --- notes (användaren -> Director/Manager) -----------------------------------

    async def note(self, owner: str, verbatim: str, interpretation: str, request_id: int | None = None) -> int:
        assert owner in OWNERS
        nid = await self.db.execute(
            "INSERT INTO notes (owner, verbatim, interpretation, request_id) VALUES (?, ?, ?, ?)",
            (owner, verbatim, interpretation, request_id),
        )
        if request_id is not None:
            await self.db.execute("UPDATE requests SET status = 'answered', answered_at = datetime('now') WHERE id = ?", (request_id,))
        return nid

    async def unprocessed(self, owner: str) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT n.*, r.what AS question FROM notes n LEFT JOIN requests r ON r.id = n.request_id "
            "WHERE n.owner = ? AND n.processed_at IS NULL ORDER BY n.id",
            (owner,),
        )
        return [dict(r) for r in rows]

    async def mark_processed(self, owner: str) -> None:
        await self.db.execute("UPDATE notes SET processed_at = datetime('now') WHERE owner = ? AND processed_at IS NULL", (owner,))
