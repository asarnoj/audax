"""Omgång 14, B: each role reviews the fast skill that is its own."""

import json
import random

import pytest

from agent import crosscheck, facit
from agent.db import Database
from agent.items import Items
from agent.pipeline.outbox import add_candidate


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


async def drop(db, title, reason, source="stream:x", leaned=None):
    eid = await db.execute("INSERT INTO events (source, kind, dedupe_key, payload) VALUES (?, 'event', ?, '{}')", (source, title))
    cid = await add_candidate(db, "event", title, f"{title} brief", {"reason": reason}, event_id=eid)
    await db.execute("UPDATE candidates SET status = 'dropped', tier = 'drop' WHERE id = ?", (cid,))
    await db.insert("decisions", candidate_id=cid, module="triage@1", tier="drop", reason=reason)
    if leaned:
        await facit.record(db, [f"#U{leaned}"], cid, "triage@1", "drop")
    return cid


async def test_director_sees_some_drops_at_random_and_every_one_whose_belief_changed(db):
    # 10 Nov: triage lets about forty a day go and nobody looked; the test cases can never show a drop
    java = await Items(db).create("belief", "Kan inte Java", "assistant", kind="fact", source={"type": "owner", "quote": "inte java"})
    leaned = await drop(db, "Kotlin-jobb", "Javaroll", leaned=java)
    others = [await drop(db, f"Brus {i}", "brus") for i in range(12)]
    await db.execute("UPDATE belief_uses SET created_at = datetime('now', '-1 hour')")
    await Items(db).update(java, "assistant", title="Kan inte Java, men Kotlin går bra")
    text, shown = await crosscheck.triage_sample(db, "2000-01-01 00:00:00", random.Random(1))
    assert shown[0] == leaned and len(shown) == 1 + crosscheck.AT_RANDOM and set(shown[1:]) <= set(others)
    assert "its belief has changed since" in text and f"#U{java}" in text and "13 dropped" in text
    assert await crosscheck.triage_sample(db, "2999-01-01 00:00:00") == ("", [])   # nothing since: nothing to look at


async def test_a_wrong_drop_comes_back_to_the_desk_and_counts_against_what_it_leaned_on(db):
    frame = await Items(db).create("belief", "Inga event utanför Stockholm", "director", kind="preference", owner="director",
                                   source={"type": "inferred"})
    wrong = await drop(db, "Hackathon online", "utanför Stockholm", leaned=frame)
    gone = await drop(db, "Passerat event", "brus")
    await db.execute("UPDATE candidates SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (gone,))
    review = {"checked": 3, "notes": "x", "wrong": [{"id": f"#F{wrong}", "why": "Online räknas"}, {"id": f"#F{gone}", "why": "y"},
                                                    {"id": "#F99999", "why": "inte i urvalet"}]}
    changes = await crosscheck.apply_review(db, review, [wrong, gone], "2026-11-11T10:00:00+00:00")
    assert changes == ["stickprov på triagen: 3 granskade", f"#F{wrong} fel att släppa, tillbaka till desken",
                       f"#F{gone} fel att släppa, men inte längre aktuellt"]
    row = await db.fetchone("SELECT status, features FROM candidates WHERE id = ?", (wrong,))
    features = json.loads(row["features"])
    assert row["status"] == "pending" and "Online räknas" in features["returned"] and features["judged_again"]
    await db.execute("UPDATE items SET updated_at = datetime('now', '-1 day') WHERE id = ?", (frame,))
    assert "befanns fel att släppa" in await facit.for_review(db, "director")
    assert await db.kv_get(crosscheck.KEY) == "2026-11-11T10:00:00+00:00"


async def test_researcher_sees_why_its_missions_finds_were_dropped(db):
    await drop(db, "Hackathon i München", "utanför Stockholm", source="research:events")
    await drop(db, "Nyhet", "brus", source="stream:x")
    text = await crosscheck.mission_drops(db)
    assert "events: 1 släppta" in text and "utanför Stockholm" in text and "Nyhet" not in text
