from datetime import datetime

import pytest

from agent import assumptions, trail
from agent.config import load_settings
from agent.db import Database
from agent.due import review
from agent.items import Items


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    trail.attach(d)
    yield d
    Items.watchers = []
    trail.attach(None)
    await d.close()


async def test_the_quiet_hours_are_a_starting_assumption_the_engine_follows_and_that_can_change(db):
    settings = load_settings()
    Items.watchers = [assumptions.watcher(settings)]
    await assumptions.ensure(db, settings)
    b = await assumptions.current(db, "quiet_hours")
    assert b["source"]["type"] == "assumed" and b["owner"] == "manager" and b["value"] == f"{settings.quiet_hours.start:%H:%M}-{settings.quiet_hours.end:%H:%M}"
    assert "Antaganden ditt arbete vilar på" in await review(db, "manager", datetime.now())
    # the owner says otherwise: a new belief replaces it, and the engine follows
    new = await Items(db).create("belief", "Nattro från 22", "assistant", kind="preference", owner="manager",
                                 data={"setting": "quiet_hours", "value": "22:00-06:30"}, source={"type": "owner", "quote": "inte efter ca 22"})
    await Items(db).update(b["id"], "assistant", status="superseded", note=f"ersatt av #U{new}")
    assert f"{settings.quiet_hours.start:%H:%M}" == "22:00"
    assert f"#U{b['id']} " not in await review(db, "manager", datetime.now())   # confirmed by the owner now
    await assumptions.ensure(db, settings)
    assert len(await Items(db).find("belief", status="current", where="json_extract(data, '$.setting') = 'quiet_hours'")) == 1   # not made again
    assert len(await Items(db).find("belief", status="current")) == len(assumptions.SETTINGS)


async def test_managers_overview_shows_what_the_behaviour_says(db):
    from agent.due import pulse
    from zoneinfo import ZoneInfo

    for h in (1, 30, 31):
        await db.execute("INSERT INTO messages (telegram_message_id, direction, text, created_at) VALUES (1, 'in', 'något nytt?', datetime('now', ?))", (f"-{h} hours",))
    await db.execute("INSERT INTO feedback (signal, value) VALUES ('shelf_pull', 'F12')")
    text = await pulse(db, datetime.now(ZoneInfo("Europe/Stockholm")))
    assert "startade själv 2 samtal" in text and "bad om mer" in text and "1 gånger" in text


async def test_a_setting_is_tried_for_a_while_then_settled_and_the_engine_follows_each_step(db):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    settings = load_settings()
    await assumptions.ensure(db, settings)
    b = await assumptions.current(db, "daily_contacts")
    now = datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("Europe/Stockholm"))
    review = await assumptions.for_review(db, "manager", now)
    assert "VÄLJ HUR" in review and "prövas bara genom att fråga" in review   # the quiet hours are asked about, not tried

    out = await assumptions.planned(db, "manager", [
        {"id": f"#U{b['id']}", "plan": "test", "value": "5", "days": 10, "why": "hen ber om mer"}], settings, now)
    assert "prövas" in out[0] and settings.daily_contacts == 5
    exp = await assumptions.trial(db, "daily_contacts")
    assert (await assumptions.standing(db, b, "2026-10-30"))[0] == "trying"
    # one trial at a time, and the quiet hours aren't tried by changing them
    other = await assumptions.current(db, "message_length")
    quiet = await assumptions.current(db, "quiet_hours")
    out = await assumptions.planned(db, "manager", [
        {"id": f"#U{other['id']}", "plan": "test", "value": "300", "why": "x"},
        {"id": f"#U{quiet['id']}", "plan": "test", "value": "23:00-07:00", "why": "x"}], settings, now)
    assert "en prövning åt gången" in out[0] and "fråga i stället" in out[1]

    # the period ends: back to the old value, and the review shows the numbers to decide on
    later = now + timedelta(days=10, hours=1)
    await assumptions.apply(db, settings, later)
    assert settings.daily_contacts == 3
    assert "Prövningen" in await assumptions.for_review(db, "manager", later) and "före · under" in await assumptions.for_review(db, "manager", later)
    out = await assumptions.settled(db, "manager", [{"experiment": f"#E{exp['id']}", "outcome": "adopt", "why": "fler svar"}], settings, later)
    assert "gäller nu" in out[0] and settings.daily_contacts == 5
    new = await assumptions.current(db, "daily_contacts")
    assert new["source"]["type"] == "observed" and new["value"] == "5"
    assert "Prövningen" not in await assumptions.for_review(db, "manager", later)


async def test_an_assumption_can_be_asked_about_or_left_until_a_date(db):
    from zoneinfo import ZoneInfo

    settings = load_settings()
    await assumptions.ensure(db, settings)
    quiet = await assumptions.current(db, "quiet_hours")
    bundles = await assumptions.current(db, "bundle_times")
    now = datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("Europe/Stockholm"))
    out = await assumptions.planned(db, "manager", [
        {"id": f"#U{quiet['id']}", "plan": "ask", "question": "Är 22 en bra gräns på kvällen?", "why": "nattron är gissad"},
        {"id": f"#U{bundles['id']}", "plan": "later", "until": "2026-11-10", "why": "en prövning pågår snart"}], settings, now)
    assert "#Q" in out[0] and "2026-11-10" in out[1]
    assert (await assumptions.standing(db, quiet, "2026-10-30"))[0] == "trying"
    assert (await assumptions.standing(db, await Items(db).get(bundles["id"]), "2026-10-30"))[0] == "later"
    q = (await Items(db).find("question"))[0]
    assert q["to"] == "owner" and "prövar antagandet" in q["why"]


async def test_bundles_and_the_days_contacts(db):
    from datetime import time, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Stockholm")
    times = [time(9, 30), time(17, 0)]
    assert assumptions.next_bundle(times, datetime(2026, 10, 30, 12, 0, tzinfo=tz)).hour == 17
    assert assumptions.next_bundle(times, datetime(2026, 10, 30, 9, 35, tzinfo=tz)) is None   # a bundle moment: it goes
    assert assumptions.next_bundle(times, datetime(2026, 10, 30, 18, 0, tzinfo=tz)).day == 31
    assert assumptions.next_bundle([], datetime(2026, 10, 30, 12, 0, tzinfo=tz)) is None
    for minutes in (0, 1, 60):   # a message in two parts is one contact
        await db.execute("INSERT INTO messages (telegram_message_id, direction, origin, text, created_at) "
                         "VALUES (1, 'out', 'proactive', 'x', datetime('now', ?))", (f"-{minutes} minutes",))
    assert await assumptions.contacts_today(db, datetime.now(tz)) == 2
    m = await assumptions.measure(db, datetime.now(tz) - timedelta(days=1), datetime.now(tz) + timedelta(minutes=1))
    assert m["egna kontakter/dag"] == 2.0 and m["kontakter som fick svar"] == "0/2"
