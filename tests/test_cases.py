import json

import pytest

from agent import cases, routines
from agent.db import Database
from agent.items import Items


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    Items.watchers = [routines.watch]
    await routines.refresh(d)
    yield d
    Items.watchers = []
    await d.close()


async def seen(db, source, title, url, status, when="2026-10-20 10:00:00"):
    e = await db.execute("INSERT INTO events (source, kind, dedupe_key, payload, created_at) VALUES (?, 'news', ?, ?, ?)",
                         (source, url, json.dumps({"title": title, "url": url}), when))
    c = await db.execute("INSERT INTO candidates (event_id, kind, title, body, status) VALUES (?, 'news', ?, '', ?)", (e, title, status))
    return e, c


async def test_what_mattered_is_caught_dropped_missed_or_before_and_shown_per_method(db):
    await routines.put_stream(db, {"id": "feed-a", "type": "rss", "url": "https://a"}, "researcher")
    await seen(db, "stream:feed-a", "Collide hackathon at KTH Innovation", "https://kth.example/collide", "shelved")
    _, dropped = await seen(db, "stream:feed-a", "Armada exhibitors list published", "https://armada.example/x", "dropped")
    await db.execute("INSERT INTO decisions (candidate_id, module, tier, reason) VALUES (?, 'dispatcher@1', 'drop', 'ingen koppling')", (dropped,))
    items = Items(db)
    await items.create("aim", "KTH Innovation Collide", "director", kind="pursuit", data={"url": "https://kth.example/collide"},
                       source={"type": "owner"})
    await items.create("aim", "Armada", "director", kind="pursuit", data={"url": "https://armada.example/x"}, source={"type": "owner"})
    await items.create("aim", "Utbyte", "director", kind="pursuit", data={"url": "https://kth.example/utbyte"}, source={"type": "owner"})
    w = await items.create("wanted", "Årets kort", "assistant", kind="request", owner="researcher", source={"type": "owner"},
                           data={"asked": "Swishtävling", "past": json.dumps([{"date": "2024-11-01", "title": "Swish kort 2024", "url": "https://p/2024"}])})
    await items.create("wanted", "Ett open call", "assistant", kind="miss", owner="researcher", source={"type": "owner"},
                       data={"asked": "det fanns ett", "missed_url": "https://open.example/call"})
    assert await cases.collect(db) == 5
    status = {r["url"]: (r["status"], r["method"]) for r in await db.fetchall("SELECT url, status, method FROM search_cases")}
    assert status["https://kth.example/collide"] == ("caught", "feed")
    assert status["https://armada.example/x"][0] == "dropped"    # seen, let go: judgement
    assert status["https://kth.example/utbyte"][0] == "missed"   # never seen: search
    assert status["https://p/2024"][0] == "before"               # before anything was watched
    assert status["https://open.example/call"][0] == "missed"
    text = await cases.report(db)
    assert "Of 4 that exist whether or not the search saw them" in text and "1 caught, 1 seen but dropped, 2 never seen" in text
    assert "Caught by: feed 1" in text
    assert "dropped: ingen koppling" in text and f"#J{w}" not in text   # a round before Audax isn't counted
    assert len(await cases.unreviewed(db)) == 3
    await cases.mark_reviewed(db)
    assert await cases.unreviewed(db) == [] and await cases.collect(db) == 0
    await seen(db, "research:x", "Utbytesstudier 2027", "https://kth.example/utbyte", "sent")   # found after all
    await db.execute("UPDATE search_cases SET updated_at = datetime('now', '-2 days')")
    assert await cases.collect(db) == 1
    assert (await db.fetchone("SELECT status, method FROM search_cases WHERE url = 'https://kth.example/utbyte'"))["method"] == "search mission"
