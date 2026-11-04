from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent import ledger
from agent.db import Database

TZ = "Europe/Stockholm"


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


async def msg(db, direction, minutes_ago, origin=None, text="x"):
    return await db.execute("INSERT INTO messages (telegram_message_id, direction, origin, text, created_at) "
                            "VALUES (1, ?, ?, ?, datetime('now', ?))", (direction, origin, text, f"-{minutes_ago} minutes"))


async def test_each_contact_carries_its_context_and_how_it_landed(db):
    ids = []
    for key, kind in (("k1", "opportunity"), ("k2", "event")):
        e = await db.execute("INSERT INTO events (source, kind, dedupe_key, payload) VALUES ('s', 'news', ?, '{}')", (key,))
        ids.append(await db.execute("INSERT INTO candidates (event_id, kind, title, body) VALUES (?, ?, 'a', '')", (e, kind)))
    c1, c2 = ids
    first = await msg(db, "out", 300, "proactive", "x" * 400)
    await msg(db, "out", 299, "proactive", "y" * 100)          # the same contact, sent in two parts
    for c in (c1, c2):
        await db.execute("INSERT INTO message_candidates (message_id, candidate_id) VALUES (?, ?)", (first, c))
    await msg(db, "in", 280)                                      # answered after 19 minutes
    q = await msg(db, "out", 100, "proactive")
    await db.execute("INSERT INTO message_refs (message_id, ref) VALUES (?, '#Q5')", (q,))
    await db.execute("INSERT INTO feedback (signal, value, created_at) VALUES ('text', 'negative:amount', datetime('now', '-90 minutes'))")
    cs = await ledger.contacts(db, TZ, 21)
    assert [c["kind"] for c in cs] == ["fynd", "fråga"]
    first_c = cs[0]
    assert first_c["length"] == 500 and first_c["items"] == 2 and first_c["answered"] and first_c["minutes"] == 19
    assert cs[1]["nth"] == 2 and cs[1]["too_much"] and not cs[1]["answered"]


async def test_the_summary_shows_errors_both_ways_and_what_was_never_tried(db):
    for minutes in (600, 300):
        await msg(db, "out", minutes, "proactive")
    for _ in range(3):
        await db.execute("INSERT INTO feedback (signal, value) VALUES ('shelf_pull', 'pulled')")
    text = await ledger.summary(db, TZ)
    assert "Egna kontakter: 2" in text and "(för få)" in text
    assert "höll tillbaka men ägaren ville ha det 3 gånger" in text and "skickade men ägaren ville inte 0" in text
    assert "Aldrig prövat" in text and "fler än" in text and "ett meddelande som samlar flera saker" in text
    desk = await ledger.for_desk(db, TZ)
    assert "held back what the owner then wanted 3 times" in desk
