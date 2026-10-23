"""The schedule: syncs calendars, detects changes, learns which sessions apply, and reports availability.

Relevance per calendar:
- mine: every event applies (e.g. the personal Apple calendar)
- ask:  each series (e.g. "Föreläsning DD2440") is unknown until the owner says mine/not_mine/sometimes
Unknown series count as busy, to be on the safe side.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from agent.config import Settings
from agent.db import Database
from agent.sources.calendar import CalEvent, CalendarConfig, CalendarSource

log = logging.getLogger(__name__)

RELEVANCE = ("unknown", "mine", "not_mine", "sometimes")
POLL_MAX_OPTIONS = 10  # Telegram's limit is 10–12; keep one free for "none of these"
POLL_OPTION_LEN = 100


@dataclass(frozen=True)
class Slot:
    summary: str
    location: str
    start: datetime
    end: datetime
    all_day: bool
    relevance: str  # mine|unknown|sometimes
    series_id: int | None = None


@dataclass(frozen=True)
class Change:
    kind: str  # moved|cancelled|removed|added
    summary: str
    start: datetime
    old_start: datetime | None = None
    location: str = ""
    old_location: str = ""


@dataclass(frozen=True)
class Availability:
    at: datetime
    quiet: bool
    current: Slot | None
    next: Slot | None
    remaining_today: list[Slot]

    def status_line(self) -> str:
        parts = []
        if self.quiet:
            parts.append("nattro")
        if self.current:
            parts.append(f"nu: {self.current.summary} till {self.current.end:%H:%M}" + _maybe(self.current))
        else:
            parts.append("ledig nu")
        if self.next:
            same_day = self.next.start.date() == self.at.date()
            when = f"{self.next.start:%H:%M}" if same_day else f"{_weekday(self.next.start).lower()} {self.next.start:%H:%M}"
            parts.append(f"nästa: {self.next.summary} {when}" + _maybe(self.next))
        return ", ".join(parts)


def utc_iso(d: datetime) -> str:
    """Timestamps are stored as UTC ISO strings so SQL string comparison is correct across DST."""
    return d.astimezone(timezone.utc).isoformat()


def _maybe(slot: Slot) -> str:
    return {"unknown": " (osäkert om det gäller dig)", "sometimes": " (ibland)"}.get(slot.relevance, "")


class Schedule:
    def __init__(self, settings: Settings, db: Database, sources: list[CalendarSource], window_days: int = 21):
        self.settings = settings
        self.db = db
        self.sources = sources
        self.window_days = window_days
        self.tz = ZoneInfo(settings.timezone)
        self.relevance_mode = {s.config.id: s.config.relevance for s in sources}

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def set_sources(self, sources: list[CalendarSource]) -> None:
        """Live reload of the calendars in config/sources.yaml."""
        self.sources = sources
        self.relevance_mode = {s.config.id: s.config.relevance for s in sources}

    # --- sync -------------------------------------------------------------

    async def sync(self) -> list[Change]:
        """Fetches every source and returns changes to relevant upcoming events."""
        changes: list[Change] = []
        now = self.now()
        start, end = now - timedelta(days=1), now + timedelta(days=self.window_days)
        for source in self.sources:
            cid = source.config.id
            try:
                events = await source.fetch(start, end)
            except Exception as e:
                log.warning("calendar %s fetch failed: %s", cid, e)
                continue
            baseline = not await self.db.kv_get(f"calendar.synced.{cid}", False)
            found = await self._store(source.config, events, now, end)
            await self.db.kv_set(f"calendar.synced.{cid}", True)
            await self.db.kv_set(f"calendar.last_sync.{cid}", now.isoformat())
            log.info("calendar %s: %d events, %d changes%s", cid, len(events), len(found), " (baseline)" if baseline else "")
            if not baseline:
                changes.extend(found)
        return changes

    async def _store(self, config: CalendarConfig, events: list[CalEvent], now: datetime, end: datetime) -> list[Change]:
        cid = config.id
        rows = await self.db.fetchall(
            "SELECT * FROM calendar_events WHERE calendar = ? AND status != 'removed' AND end >= ?", (cid, utc_iso(now))
        )
        old = {r["key"]: r for r in rows}
        changes: list[Change] = []
        seen: set[str] = set()

        for ev in events:
            seen.add(ev.key)
            series_id = await self._ensure_series(cid, ev)
            status = "cancelled" if ev.cancelled else "active"
            prev = old.get(ev.key)
            if prev is not None and ev.end >= now and await self._is_relevant(cid, series_id):
                prev_start = datetime.fromisoformat(prev["start"]).astimezone(self.tz)
                if status == "cancelled" and prev["status"] != "cancelled":
                    changes.append(Change("cancelled", ev.summary, ev.start, location=ev.location))
                elif prev_start != ev.start or (prev["location"] or "") != ev.location:
                    changes.append(
                        Change("moved", ev.summary, ev.start, old_start=prev_start, location=ev.location, old_location=prev["location"] or "")
                    )
            await self.db.execute(
                "INSERT INTO calendar_events (calendar, key, series_key, summary, location, description, start, end, all_day, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(calendar, key) DO UPDATE SET series_key=excluded.series_key, summary=excluded.summary, "
                "location=excluded.location, description=excluded.description, start=excluded.start, end=excluded.end, "
                "all_day=excluded.all_day, status=excluded.status, last_seen=datetime('now')",
                (cid, ev.key, ev.series_key, ev.summary, ev.location, ev.description, utc_iso(ev.start), utc_iso(ev.end), int(ev.all_day), status),
            )

        # Upcoming events that disappeared from the source were removed (TimeEdit often deletes instead of cancelling).
        for key, r in old.items():
            r_start = datetime.fromisoformat(r["start"]).astimezone(self.tz)
            if key in seen or r_start > end:
                continue
            await self.db.execute("UPDATE calendar_events SET status = 'removed' WHERE calendar = ? AND key = ?", (cid, key))
            series = await self.db.fetchone("SELECT id FROM calendar_series WHERE calendar = ? AND series_key = ?", (cid, r["series_key"]))
            if r["status"] == "active" and r_start > now and await self._is_relevant(cid, series["id"] if series else None):
                changes.append(Change("removed", r["summary"], r_start, location=r["location"] or ""))
        return changes

    async def _ensure_series(self, calendar: str, ev: CalEvent) -> int:
        row = await self.db.fetchone("SELECT id FROM calendar_series WHERE calendar = ? AND series_key = ?", (calendar, ev.series_key))
        if row:
            return row["id"]
        initial = "mine" if self.relevance_mode.get(calendar) == "mine" else "unknown"
        return await self.db.execute(
            "INSERT INTO calendar_series (calendar, series_key, label, relevance) VALUES (?, ?, ?, ?)",
            (calendar, ev.series_key, ev.summary, initial),
        )

    async def _is_relevant(self, calendar: str, series_id: int | None) -> bool:
        if self.relevance_mode.get(calendar) == "mine":
            return True
        if series_id is None:
            return False
        row = await self.db.fetchone("SELECT relevance FROM calendar_series WHERE id = ?", (series_id,))
        return row is not None and row["relevance"] in ("mine", "sometimes")

    # --- reading ----------------------------------------------------------

    async def slots(self, start: datetime, end: datetime, include_not_mine: bool = False) -> list[Slot]:
        rows = await self.db.fetchall(
            "SELECT e.*, s.id AS series_id, s.relevance FROM calendar_events e "
            "LEFT JOIN calendar_series s ON s.calendar = e.calendar AND s.series_key = e.series_key "
            "WHERE e.status = 'active' AND e.end > ? AND e.start < ? ORDER BY e.start",
            (utc_iso(start), utc_iso(end)),
        )
        out = []
        for r in rows:
            rel = r["relevance"] or "unknown"
            if rel == "not_mine" and not include_not_mine:
                continue
            out.append(
                Slot(
                    summary=r["summary"],
                    location=r["location"] or "",
                    start=datetime.fromisoformat(r["start"]).astimezone(self.tz),
                    end=datetime.fromisoformat(r["end"]).astimezone(self.tz),
                    all_day=bool(r["all_day"]),
                    relevance=rel,
                    series_id=r["series_id"],
                )
            )
        return out

    async def availability(self, at: datetime | None = None) -> Availability:
        now = at or self.now()
        end_of_day = now.replace(hour=23, minute=59)
        timed = [s for s in await self.slots(now - timedelta(hours=12), now + timedelta(days=2)) if not s.all_day]
        current = next((s for s in timed if s.start <= now < s.end), None)
        upcoming = [s for s in timed if s.start > now]
        return Availability(
            at=now,
            quiet=self.settings.quiet_hours.contains(now.time()),
            current=current,
            next=upcoming[0] if upcoming else None,
            remaining_today=[s for s in timed if s.end > now and s.start <= end_of_day],
        )

    async def free_windows(self, at: datetime | None = None, min_minutes: int = 15) -> list[tuple[datetime, datetime]]:
        """Free time from now until quiet hours start, between sessions that are (or might be) the owner's."""
        now = at or self.now()
        q = self.settings.quiet_hours
        day_end = now.replace(hour=q.start.hour, minute=q.start.minute, second=0, microsecond=0)
        if q.contains(now.time()) or day_end <= now:
            return []
        busy = sorted((s.start, s.end) for s in await self.slots(now, day_end) if not s.all_day)
        windows, cursor = [], now
        for start, end in busy:
            if start > cursor:
                windows.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < day_end:
            windows.append((cursor, day_end))
        return [(a, b) for a, b in windows if (b - a).total_seconds() >= min_minutes * 60]

    async def format_days(self, days: int = 2, include_not_mine: bool = False) -> str:
        now = self.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        slots = await self.slots(start, start + timedelta(days=days), include_not_mine)
        if not slots:
            return "Inget i schemat."
        lines, day = [], None
        for s in slots:
            if s.start.date() != day:
                day = s.start.date()
                lines.append(f"\n{_weekday(s.start)} {s.start.day}/{s.start.month}")
            when = "heldag" if s.all_day else f"{s.start:%H:%M}–{s.end:%H:%M}"
            tag = {"unknown": " [?]", "sometimes": " [ibland]", "not_mine": " [inte mitt]"}.get(s.relevance, "")
            where = f" · {s.location}" if s.location else ""
            ref = f" (serie {s.series_id})" if s.series_id else ""
            lines.append(f"- {when} {s.summary}{where}{tag}{ref}")
        return "\n".join(lines).strip()

    # --- relevance --------------------------------------------------------

    async def set_relevance(self, series_id: int, relevance: str, note: str | None = None) -> bool:
        if relevance not in RELEVANCE:
            raise ValueError(relevance)
        await self.db.execute(
            "UPDATE calendar_series SET relevance = ?, note = COALESCE(?, note), updated_at = datetime('now') WHERE id = ?",
            (relevance, note, series_id),
        )
        changed = (await self.db.fetchone("SELECT changes() AS n"))["n"] > 0
        for poll in await self.db.fetchall("SELECT poll_id, payload FROM polls WHERE kind = 'schedule_relevance' AND answered_at IS NULL"):
            if series_id in json.loads(poll["payload"]).get("series_ids", []):   # answered in the conversation
                await self.db.execute("UPDATE polls SET answered_at = datetime('now'), answer = 'i chatten' WHERE poll_id = ?", (poll["poll_id"],))
        return changed

    async def unknown_series(self) -> list[dict]:
        """Unknown series with an upcoming session, soonest first: these are worth asking about."""
        rows = await self.db.fetchall(
            "SELECT s.id, s.label, MIN(e.start) AS next_start FROM calendar_series s "
            "JOIN calendar_events e ON e.calendar = s.calendar AND e.series_key = s.series_key "
            "WHERE s.relevance = 'unknown' AND e.status = 'active' AND e.start > ? "
            "GROUP BY s.id ORDER BY next_start",
            (utc_iso(self.now()),),
        )
        return [dict(r) for r in rows]

    async def next_question(self) -> dict | None:
        """A poll about unknown series, or None if one is already open or nothing is unknown."""
        if await self.db.fetchone("SELECT 1 FROM polls WHERE kind = 'schedule_relevance' AND answered_at IS NULL AND created_at >= datetime('now', '-3 days')"):
            return None
        unknown = (await self.unknown_series())[:POLL_MAX_OPTIONS]
        if not unknown:
            return None
        options = [_option(u["label"], u["next_start"], self.tz) for u in unknown]
        return {
            "question": "Vilka av de här passen i TimeEdit gäller dig? (Kryssa i alla som gäller.)",   # as a poll only without a chat
            "options": options + ["Inga av dessa"],
            "series_ids": [u["id"] for u in unknown],
        }

    async def record_poll(self, poll_id: str, message_row_id: int | None, question: dict) -> None:
        await self.db.execute(
            "INSERT INTO polls (poll_id, kind, payload, message_id) VALUES (?, 'schedule_relevance', ?, ?)",
            (poll_id, json.dumps({"series_ids": question["series_ids"]}), message_row_id),
        )
        marks = ",".join("?" * len(question["series_ids"]))
        await self.db.execute(f"UPDATE calendar_series SET asked_at = datetime('now') WHERE id IN ({marks})", tuple(question["series_ids"]))

    async def handle_poll_answer(self, poll_id: str, option_ids: list[int]) -> str | None:
        row = await self.db.fetchone("SELECT kind, payload FROM polls WHERE poll_id = ?", (poll_id,))
        if row is None or row["kind"] != "schedule_relevance":
            return None
        series_ids = json.loads(row["payload"])["series_ids"]
        if not option_ids:  # answer retracted
            await self.db.execute("UPDATE polls SET answer = NULL, answered_at = NULL WHERE poll_id = ?", (poll_id,))
            return None
        chosen = {series_ids[i] for i in option_ids if i < len(series_ids)}
        for sid in series_ids:
            await self.set_relevance(sid, "mine" if sid in chosen else "not_mine")
        await self.db.execute(
            "UPDATE polls SET answer = ?, answered_at = datetime('now') WHERE poll_id = ?", (json.dumps(option_ids), poll_id)
        )
        await self.db.execute(
            "INSERT INTO feedback (signal, value, note) VALUES ('poll', 'schedule_relevance', ?)",
            (f"{len(chosen)} av {len(series_ids)} pass gäller",),
        )
        return f"Tack! Noterat att {len(chosen)} av {len(series_ids)} gäller dig. Du kan alltid rätta mig i chatten."


def _weekday(d: datetime) -> str:
    return ["Mån", "Tis", "Ons", "Tor", "Fre", "Lör", "Sön"][d.weekday()]


def _option(label: str, next_start: str, tz: ZoneInfo) -> str:
    d = datetime.fromisoformat(next_start).astimezone(tz)
    suffix = f" ({_weekday(d).lower()} {d:%H:%M})"
    room = POLL_OPTION_LEN - len(suffix)
    return (label if len(label) <= room else label[: room - 1] + "…") + suffix


def describe_change(c: Change) -> str:
    when = f"{_weekday(c.start).lower()} {c.start.day}/{c.start.month} {c.start:%H:%M}"
    if c.kind == "cancelled":
        return f"Inställt: {c.summary} ({when})."
    if c.kind == "removed":
        return f"Borttaget ur schemat: {c.summary} ({when}). Kan vara inställt."
    if c.kind == "moved":
        parts = []
        if c.old_start and c.old_start != c.start:
            parts.append(f"flyttat från {_weekday(c.old_start).lower()} {c.old_start:%H:%M} till {when}")
        if c.old_location != c.location:
            parts.append(f"ny sal: {c.location or '?'} (var {c.old_location or '?'})")
        return f"Ändrat: {c.summary}: " + ", ".join(parts) + "."
    return f"Nytt: {c.summary} ({when})."
