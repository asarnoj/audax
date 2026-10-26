from datetime import datetime, timedelta

import pytest

from agent import refs, threads, trail
from agent.db import Database
from agent.due import review
from agent.items import Items
from agent.office_tools import office_tools


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    trail.attach(d)
    Items.watchers = [threads.watcher("Europe/Stockholm")]
    yield d
    Items.watchers = []
    trail.attach(None)
    await d.close()


def text(out):
    return out["content"][0]["text"]


async def test_a_followed_thing_comes_up_on_its_date_or_when_something_new_happens(db):
    goal = await Items(db).create("aim", "Sommarjobb 2027", "director", kind="goal", data={"why": "Komma in i branschen"})
    director = {t.name: t for t in office_tools(db, "director")}
    later = f"{datetime.now() + timedelta(days=40):%Y-%m-%d}"
    out = await director["follow"].handler({"id": f"#S{goal}", "when": later, "why": "Ansökningarna öppnar i januari; fråga om CV:t i december"})
    assert "Following" in text(out)
    assert await threads.due(db, datetime.now()) == []
    # another role adds to it: news, and it comes up now
    await Items(db).create("step", "Kolla Spotifys praktik", "director", owner="researcher", parent=goal, review_at=later,
                           data={"action": "research", "done_when": "Datum"})
    await Items(db).update((await Items(db).find("step"))[0]["id"], "researcher", status="done", data={"result": "Öppnar 15 jan"})
    [f] = await threads.due(db, datetime.now())
    assert "Öppnar 15 jan" in await threads.thread(db, goal) and "Kolla Spotifys" in f["news"]
    assert "Tråd att se över nu" in await review(db, "director", datetime.now())


async def test_the_owners_words_and_the_office_contacts_show_in_the_thread(db):
    goal = await Items(db).create("aim", "Barnboken", "director", kind="project")
    await threads.follow(db, "director", goal, "2030-01-01", "Hur det går med boken")
    out = await db.execute("INSERT INTO messages (telegram_message_id, direction, text, origin, created_at) VALUES (1, 'out', 'Hur går det med barnboken?', 'proactive', datetime('now', '-2 days'))")
    await refs.link(db, [out], {f"#S{goal}"})
    reply = await db.execute("INSERT INTO messages (telegram_message_id, direction, text, created_at) VALUES (2, 'in', 'Bra, skissar kapitel tre', datetime('now', '-2 days', '+1 hour'))")
    await refs.link(db, [reply], {f"#S{goal}"})
    t = await threads.thread(db, goal)
    assert "skissar kapitel tre" in t and "Hur går det med barnboken?" in t and "troligen svar" in t
    [f] = await threads.followed(db, "director")
    assert "ägaren" in (f["news"] or "")   # their words are news in the thread


async def test_a_close_deadline_is_news_once(db):
    o = await Items(db).create("opportunity", "Bologna Illustrators Exhibition", "researcher", kind="event", owner="researcher",
                               data={"url": "https://x", "organization": "BCBF", "deadline": f"{datetime.now() + timedelta(days=2):%Y-%m-%d}"})
    await threads.follow(db, "director", o, "2030-01-01", "Passar barnboken")
    await threads.deadlines(db, datetime.now())
    await threads.deadlines(db, datetime.now())
    [f] = await threads.followed(db)
    assert f["news"].count("sista dag") == 1 and f["look_at"] < "2030"
