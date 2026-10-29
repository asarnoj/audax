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
    assert "Antaganden ditt arbete vilar på" not in await review(db, "manager", datetime.now())   # confirmed by the owner now
    await assumptions.ensure(db, settings)
    assert len(await Items(db).find("belief", status="current")) == 1   # not made again


async def test_managers_overview_shows_what_the_behaviour_says(db):
    from agent.due import pulse
    from zoneinfo import ZoneInfo

    for h in (1, 30, 31):
        await db.execute("INSERT INTO messages (telegram_message_id, direction, text, created_at) VALUES (1, 'in', 'något nytt?', datetime('now', ?))", (f"-{h} hours",))
    await db.execute("INSERT INTO feedback (signal, value) VALUES ('shelf_pull', 'F12')")
    text = await pulse(db, datetime.now(ZoneInfo("Europe/Stockholm")))
    assert "startade själv 2 samtal" in text and "bad om mer" in text and "1 gånger" in text
