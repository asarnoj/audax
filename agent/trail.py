"""Who did what (docs/MODEL.md, "ID:n och uppslag"): the run behind every row, and the office's event log.

Every Claude run has an id (#R). While it runs, and while its caller applies what it returned, `current_run`
holds that id; the database stamps it on the rows written then (posts, decisions, findings, messages, bookings,
pursuits), and memory saves carry it in their commit message. So what a run did is looked up, never guessed.

The event log (office_events) records what happens in the office, in structured fields, where it happens: a run
starting or ending, something new printed, triage keeping or throwing away, the dispatcher placing a finding, a
post, a message, a booking, the board being taken from, a memory save. The office page follows this log.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar

log = logging.getLogger(__name__)

# The run whose work is being written. Set by ClaudeGate when a run starts and left set for the rest of the calling
# task, so what the caller applies from the run's answer is stamped with it too. Each scheduled job and each
# incoming message is its own task, so the id never leaks into unrelated work.
current_run: ContextVar[int | None] = ContextVar("current_run", default=None)

_db = None


def attach(db) -> None:
    """The database the event log is written to (set once at startup; without it, emit does nothing)."""
    global _db
    _db = db


def tag() -> str:
    """For a memory commit message: which run it came from."""
    rid = current_run.get()
    return f" [#R{rid}]" if rid else ""


async def emit(kind: str, role: str | None = None, **data) -> None:
    if _db is None:
        return
    try:
        await _db.execute(
            "INSERT INTO office_events (run_id, kind, role, data) VALUES (?, ?, ?, ?)",
            (current_run.get(), kind, role, json.dumps(data, ensure_ascii=False)),
        )
    except Exception:  # the log must never break the work it records
        log.exception("office event %s not recorded", kind)
