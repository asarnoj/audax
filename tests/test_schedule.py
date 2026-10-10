from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent.config import load_settings
from agent.db import Database
from agent.schedule import Schedule, describe_change
from agent.sources.calendar import CalendarConfig, parse_ics, series_key

TZ = ZoneInfo("Europe/Stockholm")


def ics(*events: str) -> str:
    return "BEGIN:VCALENDAR\nVERSION:2.0\n" + "".join(events) + "END:VCALENDAR\n"


def vevent(uid: str, start: datetime, hours: float, summary: str, location: str = "", extra: str = "") -> str:
    end = start + timedelta(hours=hours)
    fmt = lambda d: d.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
    return (
        f"BEGIN:VEVENT\nUID:{uid}\nDTSTART:{fmt(start)}\nDTEND:{fmt(end)}\nSUMMARY:{summary}\n"
        f"LOCATION:{location}\n{extra}END:VEVENT\n"
    )


class FakeSource:
    def __init__(self, cid: str, relevance: str):
        self.config = CalendarConfig(id=cid, type="ics", relevance=relevance, url="x")
        self.ics = ics()

    async def fetch(self, start, end):
        return parse_ics(self.config.id, self.ics, start, end, "Europe/Stockholm")


class FixedSchedule(Schedule):
    clock: datetime

    def now(self) -> datetime:
        return self.clock


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


@pytest.fixture
def now():
    return datetime(2026, 10, 12, 9, 0, tzinfo=TZ)  # a Monday


def make(db, now, *sources):
    s = FixedSchedule(load_settings(), db, list(sources))
    s.clock = now
    return s


# --- parsing --------------------------------------------------------------


def test_series_key_ignores_case_whitespace_and_cancellation():
    assert series_key("Föreläsning,  DD2440") == series_key("INSTÄLLD: föreläsning DD2440")


def test_parse_recurring_gives_stable_unique_keys(now):
    rrule = "RRULE:FREQ=WEEKLY;COUNT=3\n"
    evs = parse_ics("t", ics(vevent("a", now, 2, "Föreläsning", extra=rrule)), now - timedelta(days=1), now + timedelta(days=30), "Europe/Stockholm")
    assert len(evs) == 3 and len({e.key for e in evs}) == 3
    assert evs[0].start == now and evs[0].start.tzinfo is not None


def test_parse_all_day_and_cancelled(now):
    allday = "BEGIN:VEVENT\nUID:d\nDTSTART;VALUE=DATE:20261013\nSUMMARY:Heldag\nEND:VEVENT\n"
    cancelled = vevent("c", now + timedelta(hours=3), 1, "Inställd: Övning")
    evs = {e.key: e for e in parse_ics("t", ics(allday, cancelled), now, now + timedelta(days=3), "Europe/Stockholm")}
    assert evs["d"].all_day and evs["d"].end - evs["d"].start == timedelta(days=1)
    assert evs["c"].cancelled


# --- sync and changes -----------------------------------------------------


async def test_first_sync_is_baseline_then_changes_are_detected(db, now):
    src = FakeSource("apple", "mine")
    src.ics = ics(vevent("a", now + timedelta(hours=5), 1, "Möte", "Rum 1"), vevent("b", now + timedelta(days=2), 1, "Gym"))
    s = make(db, now, src)
    assert await s.sync() == []  # baseline: no notifications for the initial import

    src.ics = ics(vevent("a", now + timedelta(hours=6), 1, "Möte", "Rum 2"))  # moved + new room; b removed
    changes = {c.kind: c for c in await s.sync()}
    assert set(changes) == {"moved", "removed"}
    assert changes["moved"].old_start == now + timedelta(hours=5)
    assert "ny sal: Rum 2 (var Rum 1)" in describe_change(changes["moved"])
    assert changes["removed"].summary == "Gym"


async def test_changes_to_sessions_that_are_not_mine_are_ignored(db, now):
    src = FakeSource("timeedit", "ask")
    src.ics = ics(vevent("x", now + timedelta(hours=5), 2, "Övning DD2440 grupp 3"))
    s = make(db, now, src)
    await s.sync()
    src.ics = ics(vevent("x", now + timedelta(hours=7), 2, "Övning DD2440 grupp 3"))
    assert await s.sync() == []  # unknown series: no notification

    [series] = await s.unknown_series()
    await s.set_relevance(series["id"], "mine")
    src.ics = ics(vevent("x", now + timedelta(hours=8), 2, "Övning DD2440 grupp 3"))
    assert [c.kind for c in await s.sync()] == ["moved"]


# --- availability -----------------------------------------------------------


async def test_availability_and_not_mine_sessions(db, now):
    src = FakeSource("timeedit", "ask")
    src.ics = ics(
        vevent("l", now - timedelta(minutes=30), 2, "Labb"),
        vevent("f", now + timedelta(hours=3), 1, "Föreläsning"),
    )
    s = make(db, now, src)
    await s.sync()
    a = await s.availability(now)
    assert a.current.summary == "Labb" and a.current.relevance == "unknown"
    assert "osäkert" in a.status_line()

    # "Labb" already started, so only "Föreläsning" is upcoming and worth asking about
    assert [u["label"] for u in await s.unknown_series()] == ["Föreläsning"]
    lab_series = await db.fetchone("SELECT id FROM calendar_series WHERE label = 'Labb'")
    await s.set_relevance(lab_series["id"], "not_mine")
    a = await s.availability(now)
    assert a.current is None and a.next.summary == "Föreläsning"
    assert a.status_line().startswith("ledig nu, nästa: Föreläsning 12:00")


async def test_quiet_hours(db):
    s = make(db, datetime(2026, 10, 12, 23, 30, tzinfo=TZ))
    assert (await s.availability(s.clock)).quiet


# --- questions ------------------------------------------------------------


async def test_poll_flow_sets_relevance_and_only_one_open_poll(db, now):
    src = FakeSource("timeedit", "ask")
    src.ics = ics(*[vevent(f"e{i}", now + timedelta(hours=2 + i), 1, f"Kurs {i}") for i in range(3)])
    s = make(db, now, src)
    await s.sync()

    q = await s.next_question()
    assert q["options"][-1] == "Inga av dessa" and len(q["series_ids"]) == 3
    await s.record_poll("p1", None, q)
    assert await s.next_question() is None  # one open poll at a time

    reply = await s.handle_poll_answer("p1", [0, 2])
    assert "2 av 3" in reply
    rel = {r["label"]: r["relevance"] for r in await db.fetchall("SELECT label, relevance FROM calendar_series")}
    assert rel == {"Kurs 0": "mine", "Kurs 1": "not_mine", "Kurs 2": "mine"}
    assert await s.next_question() is None  # nothing unknown left


async def test_poll_none_of_these(db, now):
    src = FakeSource("timeedit", "ask")
    src.ics = ics(vevent("a", now + timedelta(hours=2), 1, "A"), vevent("b", now + timedelta(hours=3), 1, "B"))
    s = make(db, now, src)
    await s.sync()
    q = await s.next_question()
    await s.record_poll("p", None, q)
    await s.handle_poll_answer("p", [len(q["options"]) - 1])  # "Inga av dessa"
    assert {r["relevance"] for r in await db.fetchall("SELECT relevance FROM calendar_series")} == {"not_mine"}


async def test_free_windows_between_sessions_until_quiet_hours(db, now):
    src = FakeSource("apple", "mine")
    src.ics = ics(vevent("a", now + timedelta(hours=1), 2, "Möte"), vevent("b", now + timedelta(hours=3, minutes=5), 1, "Labb"))
    s = make(db, now, src)
    await s.sync()
    w = [(f"{a:%H:%M}", f"{b:%H:%M}") for a, b in await s.free_windows(now)]
    # 09–10 free, 10–12 meeting, 12:00–12:05 too short, 12:05–13:05 lab, then free until 23:00
    assert w == [("09:00", "10:00"), ("13:05", "23:00")]
