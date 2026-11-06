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


async def test_only_what_the_owner_said_binds_and_the_quiet_hours_protect(db):
    settings = load_settings()
    Items.watchers = [assumptions.watcher(settings)]
    await assumptions.ensure(db, settings)
    await assumptions.apply(db, settings)
    assert settings.binding == {"quiet_hours"}   # a guess guides; the night is protected whoever set it
    text = await assumptions.lines(db, settings)
    assert "nattro" in text and "BINDS (protects" in text and "egna kontakter per dag: 3 · understanding" in text
    old = await assumptions.current(db, "daily_contacts")
    new = await Items(db).create("belief", "Högst en om dagen", "assistant", kind="preference", owner="manager",
                                 data={"setting": "daily_contacts", "value": "1"}, source={"type": "owner", "quote": "max en om dagen"})
    await Items(db).update(old["id"], "assistant", status="superseded", note=f"ersatt av #U{new}")
    assert settings.daily_contacts == 1 and assumptions.binds(settings, "daily_contacts")   # their own words: a rule


async def test_manager_moves_its_understanding_but_never_what_the_owner_said(db):
    from datetime import time

    settings = load_settings()
    await assumptions.ensure(db, settings)
    out = await assumptions.understood(db, "manager", [
        {"setting": "daily_contacts", "value": "5", "why": "han hämtar ett om dagen från hyllan"},
        {"setting": "bundle_times", "value": "17:00", "why": "kvällarna får svar"},
        {"setting": "message_length", "value": "lång", "why": "x"}], settings)
    assert settings.daily_contacts == 5 and settings.bundle_times == [time(17, 0)] and "kan inte läsa" in out[-1]
    b = await assumptions.current(db, "daily_contacts")
    assert b["source"]["type"] == "inferred" and not assumptions.binds(settings, "daily_contacts")   # still guidance
    await assumptions.understood(db, "manager", [{"setting": "bundle_times", "value": "inga", "why": "samlat gav inget"}], settings)
    assert settings.bundle_times == [] and (await assumptions.current(db, "bundle_times"))["value"] == "inga"
    own = await Items(db).create("belief", "Kort", "assistant", kind="preference", owner="manager",
                                 data={"setting": "message_length", "value": "300"}, source={"type": "owner"})
    await Items(db).update((await Items(db).find("belief", status="current", where="json_extract(data, '$.setting') = 'message_length' AND id != ?", args=(own,)))[0]["id"],
                           "assistant", status="superseded")
    out = await assumptions.understood(db, "manager", [{"setting": "message_length", "value": "900", "why": "x"}], settings)
    assert "fråga hellre" in out[0] and settings.message_length == 300


async def test_the_trials_of_omgang_8_end_once(db):
    e = await Items(db).create("experiment", "Prövar buntar", "manager", kind="improve", status="running", owner="manager",
                               review_at="2026-11-09", data={"setting": "bundle_times", "test_value": "09:00,17:00",
                                                                     "hypothesis": "h", "intervention": "i", "measure": "m"})
    assert await assumptions.end_trials(db) == [f"#E{e}"] and await assumptions.end_trials(db) == []
    assert (await Items(db).get(e))["status"] == "dropped"


async def test_bundles_and_the_days_contacts(db):
    from datetime import time
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


async def test_manager_is_told_once_that_its_trials_ended(db):
    # 5–6 Nov: the trials were dropped by the code, and Manager's working memory went on deferring to #E201
    eid = await Items(db).create("experiment", "Prövar buntar", "manager", kind="improve", status="running", owner="manager",
                                 review_at="2026-11-09", data={"setting": "bundle_times", "test_value": "09:00,17:00",
                                                               "hypothesis": "h", "intervention": "i", "measure": "m"})
    await assumptions.end_trials(db)
    pid = await assumptions.tell_trials_ended(db)
    post = await db.fetchone("SELECT sender, recipient, body FROM posts WHERE id = ?", (pid,))
    assert (post["sender"], post["recipient"]) == ("system", "manager") and f"#E{eid}" in post["body"]
    assert await assumptions.tell_trials_ended(db) is None   # once
