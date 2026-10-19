"""The schedule (docs/MODEL.md, "Schemat"): everything the office plans to contact the owner about, with a time.

Owned by Manager. The dispatcher (Manager's fast task) gives each new finding a delivery time, or shelves or drops it.
Reminders the owner asks for and promises the Assistant makes are bookings. Python delivers when the time comes, after
a last check (quiet hours, a pause, the owner busy in a session). No Claude run is needed in between: nothing waits in a
queue that is re-sorted every ten minutes.

Rules: the owner's own bookings are fixed, only the owner moves them. A booking is checked before anything is promised:
a conflict (quiet hours, a session, a pause) comes back at once, so the Assistant can ask instead of promising.
"""

from __future__ import annotations

from agent import trail

import logging
from datetime import datetime, timedelta

from agent.db import Database
from agent.schedule import Schedule, utc_iso

log = logging.getLogger(__name__)

SILENCE_KEY = "dispatch.silence_until"
FOCUS_KEY = "dispatch.focus_until"
WEEKDAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]


class Agenda:
    def __init__(self, db: Database, schedule: Schedule):
        self.db = db
        self.schedule = schedule
        self.tz = schedule.tz
        self.quiet = schedule.settings.quiet_hours

    # --- time helpers -----------------------------------------------------

    def parse(self, when: str) -> datetime | None:
        """'2026-10-16 08:00' (local) -> aware datetime."""
        try:
            d = datetime.fromisoformat(when.strip().replace("T", " "))
        except ValueError:
            return None
        return d.replace(tzinfo=self.tz) if d.tzinfo is None else d.astimezone(self.tz)

    def local(self, utc: str) -> datetime:
        return datetime.fromisoformat(utc).astimezone(self.tz)

    def show(self, d: datetime) -> str:
        return f"{WEEKDAYS[d.weekday()]} {d:%d/%m %H:%M}"

    def quiet_end(self, at: datetime) -> datetime:
        """The first moment at or after `at` that is outside quiet hours."""
        end = at.replace(hour=self.quiet.end.hour, minute=self.quiet.end.minute, second=0, microsecond=0)
        if not self.quiet.contains(at.time()):
            return at
        return end if end > at else end + timedelta(days=1)

    async def _until(self, key: str) -> datetime | None:
        v = await self.db.kv_get(key)
        return datetime.fromisoformat(v) if v and datetime.fromisoformat(v) > self.schedule.now() else None

    async def conflicts(self, at: datetime) -> list[str]:
        """What stands in the way of contacting the owner at `at` (in Swedish, for the Assistant to relay)."""
        out = []
        if at < self.schedule.now() - timedelta(minutes=1):
            out.append(f"{self.show(at)} har redan varit")
        if self.quiet.contains(at.time()):
            out.append(f"{self.show(at)} är under nattro ({self.quiet.start:%H:%M}–{self.quiet.end:%H:%M})")
        busy = (await self.schedule.availability(at)).current
        if busy is not None:
            out.append(f"då pågår {busy.summary} ({busy.start:%H:%M}–{busy.end:%H:%M})")
        for key, what in ((SILENCE_KEY, "tyst läge"), (FOCUS_KEY, "fokusläge")):
            if (until := await self._until(key)) and at < until:
                out.append(f"{what} gäller till {self.show(until)}")
        return out

    # --- bookings (reminders, promises) -------------------------------------

    async def already_booked(self, at: datetime, what: str, ref: str | None = None) -> int | None:
        """A booking that already covers this: one it refers to, or one about the same thing within a few hours of
        the same time. So a promise is booked once, however many times it is noticed."""
        if ref and ref.upper().startswith("#B") and ref[2:].isdigit():
            if await self.db.fetchone("SELECT 1 FROM bookings WHERE id = ? AND status IN ('planned', 'sending', 'sent')", (int(ref[2:]),)):
                return int(ref[2:])
        words = lambda t: {w for w in "".join(c if c.isalnum() else " " for c in t.casefold()).split() if len(w) > 3}  # noqa: E731
        mine = words(what)
        lo, hi = utc_iso(at - timedelta(hours=6)), utc_iso(at + timedelta(hours=6))
        for b in await self.db.fetchall(
            "SELECT id, ref, title, body FROM bookings WHERE status IN ('planned', 'sending', 'sent') AND due_at BETWEEN ? AND ?", (lo, hi)
        ):
            if ref and b["ref"] == ref:
                return b["id"]
            theirs = words(f"{b['title']} {b['body']}")
            if mine and len(mine & theirs) / len(mine) >= 0.5:
                return b["id"]
        return None

    async def book(
        self, kind: str, title: str, body: str, at: datetime, by: str, *, fixed: bool = False,
        latest: datetime | None = None, ref: str | None = None, confirm: bool = False, reason: str | None = None,
    ) -> tuple[int | None, list[str]]:
        """Books unless something stands in the way; with confirm=True (the owner insisted) it books anyway."""
        found = await self.conflicts(at)
        if found and not confirm:
            return None, found
        bid = await self.db.insert("bookings", kind=kind, title=title, body=body, due_at=utc_iso(at), latest_at=utc_iso(latest) if latest else None,
                                   fixed=int(fixed), requested_by=by, ref=ref, reason=reason)
        await trail.emit("book", by, ref=f"#B{bid}")
        return bid, found

    async def cancel(self, booking_id: int, by: str, reason: str = "") -> str:
        b = await self.db.fetchone("SELECT * FROM bookings WHERE id = ? AND status = 'planned'", (booking_id,))
        if b is None:
            return f"#B{booking_id} finns inte eller är redan skickad."
        if b["fixed"] and by != "assistant":
            return f"#B{booking_id} är ägarens egen bokning; bara ägaren kan stryka den."
        await self.db.execute("UPDATE bookings SET status = 'cancelled', reason = ? WHERE id = ?", (reason or f"struken av {by}", booking_id))
        return f"#B{booking_id} struken."

    async def move(self, ref: str, at: datetime, by: str, reason: str = "") -> str:
        """Moves a booking (#B) or a planned delivery (#F). The owner's own bookings move only when the owner asks."""
        kind, n = ref.strip("#")[0].upper(), int(ref.strip("#")[1:])
        if kind == "B":
            b = await self.db.fetchone("SELECT * FROM bookings WHERE id = ? AND status = 'planned'", (n,))
            if b is None:
                return f"{ref} finns inte eller är redan skickad."
            if b["fixed"] and by != "assistant":
                return f"{ref} är ägarens egen bokning; föreslå en flytt via Assistant i stället."
            if b["latest_at"] and utc_iso(at) > b["latest_at"]:
                return f"{ref} får inte flyttas efter {self.show(self.local(b['latest_at']))} (ett löfte)."
            await self.db.execute("UPDATE bookings SET due_at = ?, reason = ? WHERE id = ?", (utc_iso(at), reason, n))
        elif kind == "F":
            n_rows = await self.db.execute_count(
                "UPDATE candidates SET deliver_at = ?, updated_at = datetime('now') WHERE id = ? AND status = 'scheduled'", (utc_iso(at), n)
            )
            if not n_rows:
                return f"{ref} är inte planerad."
        else:
            return f"{ref}: okänd sort."
        return f"{ref} flyttad till {self.show(at)}."

    # --- planned deliveries of findings ---------------------------------------

    async def schedule_findings(self, ids: list[int], at: datetime) -> None:
        for cid in ids:
            await self.db.execute(
                "UPDATE candidates SET status = 'scheduled', deliver_at = ?, updated_at = datetime('now') WHERE id = ? AND status IN ('pending', 'scheduled')",
                (utc_iso(at), cid),
            )

    async def due_findings(self, now: datetime) -> list:
        return await self.db.fetchall(
            "SELECT * FROM candidates WHERE status = 'scheduled' AND deliver_at <= ? ORDER BY deliver_at", (utc_iso(now),)
        )

    async def due_bookings(self, now: datetime) -> list:
        return await self.db.fetchall("SELECT * FROM bookings WHERE status = 'planned' AND due_at <= ? ORDER BY due_at", (utc_iso(now),))

    # --- the view ----------------------------------------------------------

    async def upcoming(self, days: int = 7) -> str:
        """Everything planned, in time order: for Manager, the dispatcher, the Assistant and the web page."""
        until = utc_iso(self.schedule.now() + timedelta(days=days))
        items = []
        for r in await self.db.fetchall("SELECT * FROM candidates WHERE status = 'scheduled' AND deliver_at <= ?", (until,)):
            items.append((r["deliver_at"], f"#F{r['id']} {r['kind']}: {r['title'][:90]}"))
        for b in await self.db.fetchall("SELECT * FROM bookings WHERE status = 'planned' AND due_at <= ?", (until,)):
            label = "ägarens påminnelse, fast" if b["fixed"] else ("löfte" if b["kind"] == "promise" else b["kind"])
            latest = f", senast {self.show(self.local(b['latest_at']))}" if b["latest_at"] else ""
            items.append((b["due_at"], f"#B{b['id']} ({label}{latest}): {b['title'][:90]}" + (f" [{b['ref']}]" if b["ref"] else "")))
        items.sort()
        return "\n".join(f"- {self.show(self.local(t))} · {text}" for t, text in items) or "(nothing planned)"
