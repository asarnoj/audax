"""Calendar sources: ICS URLs (TimeEdit) and CalDAV (iCloud). Both yield normalized CalEvents."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events
import yaml

from agent.config import instance_config

log = logging.getLogger(__name__)

CANCELLED = re.compile(r"\b(inställd|inställt|cancel+ed|avbokad)\b", re.IGNORECASE)


@dataclass(frozen=True)
class CalEvent:
    calendar: str
    key: str
    series_key: str
    summary: str
    location: str
    description: str
    start: datetime  # timezone-aware
    end: datetime
    all_day: bool
    cancelled: bool


@dataclass(frozen=True)
class CalendarConfig:
    id: str
    type: str  # ics|caldav
    relevance: str  # ask|mine
    url: str = ""
    username: str = ""
    password: str = ""
    calendars: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        if self.type == "ics":
            return bool(self.url)
        return bool(self.url and self.username and self.password)


class CalendarSource(Protocol):
    config: CalendarConfig

    async def fetch(self, start: datetime, end: datetime) -> list[CalEvent]: ...


# --- config ---------------------------------------------------------------


def load_calendar_configs() -> tuple[list[CalendarConfig], dict]:
    data = (yaml.safe_load(instance_config("sources.yaml").read_text()) or {}).get("calendars") or {"sources": []}
    env = lambda key: os.getenv(key, "").strip() if key else ""  # noqa: E731
    configs = []
    for s in data["sources"]:
        configs.append(
            CalendarConfig(
                id=s["id"],
                type=s["type"],
                relevance=s.get("relevance", "mine"),
                url=s.get("url") or env(s.get("url_env")),
                username=env(s.get("username_env")),
                password=env(s.get("password_env")),
                calendars=tuple(c.strip() for c in env(s.get("calendars_env")).split(",") if c.strip()),
            )
        )
    return configs, {k: v for k, v in data.items() if k != "sources"}


def build_sources(configs: list[CalendarConfig], tz: str) -> list[CalendarSource]:
    sources: list[CalendarSource] = []
    for c in configs:
        if not c.configured:
            log.info("calendar %s not configured, skipping", c.id)
        elif c.type == "ics":
            sources.append(IcsUrlSource(c, tz))
        elif c.type == "caldav":
            sources.append(CalDavSource(c, tz))
        else:
            log.warning("unknown calendar type %s", c.type)
    return sources


# --- parsing --------------------------------------------------------------


def series_key(summary: str) -> str:
    """Groups recurring sessions: same summary text, ignoring case, whitespace and cancellation markers."""
    s = CANCELLED.sub("", summary.lower())
    return re.sub(r"[\s,;:]+", " ", s).strip(" -")


def _as_datetime(value: date | datetime, tz: ZoneInfo) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        return (value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)), False
    return datetime.combine(value, time.min, tzinfo=tz), True


def parse_ics(calendar_id: str, ics: bytes | str, start: datetime, end: datetime, tz: str) -> list[CalEvent]:
    zone = ZoneInfo(tz)
    cal = icalendar.Calendar.from_ical(ics)
    # recurring_ical_events sets RECURRENCE-ID on every instance, even one-off events. Only use it
    # in the key for genuinely recurring UIDs, so a moved one-off event keeps its key (= "moved").
    recurring = {
        str(c.get("UID"))
        for c in cal.walk("VEVENT")
        if c.get("RRULE") is not None or c.get("RDATE") is not None or c.get("RECURRENCE-ID") is not None
    }
    events: list[CalEvent] = []
    for e in recurring_ical_events.of(cal).between(start, end):
        dtstart = e.get("DTSTART")
        if dtstart is None:
            continue
        s, all_day = _as_datetime(dtstart.dt, zone)
        if e.get("DTEND") is not None:
            en, _ = _as_datetime(e.get("DTEND").dt, zone)
        elif e.get("DURATION") is not None:
            en = s + e.get("DURATION").dt
        else:
            en = s + (timedelta(days=1) if all_day else timedelta(hours=1))

        uid = str(e.get("UID") or f"{s.isoformat()}|{e.get('SUMMARY')}")
        rid = e.get("RECURRENCE-ID")
        key = f"{uid}|{rid.dt.isoformat()}" if rid is not None and uid in recurring else uid
        summary = str(e.get("SUMMARY") or "(utan titel)").strip()
        status = str(e.get("STATUS") or "").upper()
        events.append(
            CalEvent(
                calendar=calendar_id,
                key=key,
                series_key=series_key(summary),
                summary=summary,
                location=str(e.get("LOCATION") or "").strip(),
                description=str(e.get("DESCRIPTION") or "").strip(),
                start=s,
                end=en,
                all_day=all_day,
                cancelled=status == "CANCELLED" or bool(CANCELLED.search(summary)),
            )
        )

    # Guard against sources that reuse a UID for distinct instances.
    seen: dict[str, int] = {}
    unique = []
    for ev in sorted(events, key=lambda x: x.start):
        n = seen.get(ev.key, 0)
        seen[ev.key] = n + 1
        unique.append(ev if n == 0 else _with_key(ev, f"{ev.key}#{ev.start.isoformat()}"))
    return unique


def _with_key(ev: CalEvent, key: str) -> CalEvent:
    return CalEvent(**{**ev.__dict__, "key": key})


# --- sources --------------------------------------------------------------


class IcsUrlSource:
    def __init__(self, config: CalendarConfig, tz: str):
        self.config = config
        self.tz = tz

    async def fetch(self, start: datetime, end: datetime) -> list[CalEvent]:
        url = re.sub(r"^webcals?://", "https://", self.config.url)
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
        return parse_ics(self.config.id, r.content, start, end, self.tz)


class CalDavSource:
    def __init__(self, config: CalendarConfig, tz: str):
        self.config = config
        self.tz = tz

    async def fetch(self, start: datetime, end: datetime) -> list[CalEvent]:
        return await asyncio.to_thread(self._fetch_sync, start, end)

    def _fetch_sync(self, start: datetime, end: datetime) -> list[CalEvent]:
        import caldav  # imported lazily: only needed when CalDAV is configured

        events: list[CalEvent] = []
        with caldav.DAVClient(url=self.config.url, username=self.config.username, password=self.config.password, timeout=30) as client:
            for cal in client.principal().calendars():
                name = cal.get_display_name() if hasattr(cal, "get_display_name") else cal.name
                if self.config.calendars and name not in self.config.calendars:
                    continue
                try:
                    found = cal.search(event=True, start=start, end=end, expand=False)
                except Exception as e:  # e.g. subscribed/read-only calendars that reject REPORT
                    log.warning("caldav calendar %r skipped: %s", name, e)
                    continue
                for obj in found:
                    events.extend(parse_ics(self.config.id, obj.data, start, end, self.tz))
        return events
