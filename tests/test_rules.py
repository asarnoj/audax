import json

import pytest

from agent import trail
from agent.db import Database
from agent.items import Items
from agent.pipeline.outbox import add_candidate
from agent.rules import matches, screen


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    trail.attach(d)
    yield d
    trail.attach(None)
    await d.close()


async def event(db, source, kind, **payload):
    return await db.execute("INSERT INTO events (source, kind, dedupe_key, payload) VALUES (?, ?, ?, ?)",
                            (source, kind, json.dumps(payload), json.dumps(payload, ensure_ascii=False)))


async def test_code_throws_away_duplicates_what_has_passed_and_what_a_rule_says(db):
    await add_candidate(db, "news", "Machinarium 2 får en trailer", "b", {}, url="https://YouTube.com/watch?v=1")
    rule = await Items(db).create("rule", "Mentimeter: bara Sverige och student", "researcher",
                                  data={"applies_to": "mentimeter-greenhouse", "condition": json.dumps({"any": ["Toronto", "Berlin", "Sales"]}),
                                        "action": "drop", "reason": "Fel ort eller inte en studentroll"})
    ids = [
        await event(db, "stream:topic-x", "news", title="Machinarium 2 får en trailer", url="https://news.example/m2"),   # same title
        await event(db, "stream:hn", "news", title="Något annat", url="https://youtube.com/watch?v=1/"),                  # same address
        await event(db, "stream:sses", "event", title="Kurs som redan har varit", date="2026-10-01"),
        await event(db, "stream:jobtech", "opportunity", title="Junior utvecklare", deadline="2026-10-05"),
        await event(db, "stream:mentimeter-greenhouse", "opportunity", title="Sales Development Representative, Toronto"),
        await event(db, "stream:mentimeter-greenhouse", "opportunity", title="Software Engineering Intern, Stockholm"),
        await event(db, "stream:a", "news", title="En story med en lång och tydlig rubrik"),
        await event(db, "stream:b", "news", title="En story med en lång och tydlig rubrik!"),   # the same, from another source, same round
    ]
    rows = await db.fetchall("SELECT * FROM events ORDER BY id")
    decided = await screen(db, rows)
    assert set(decided) == {ids[0], ids[1], ids[2], ids[3], ids[4], ids[7]}
    assert decided[ids[4]].startswith(f"Regel #G{rule}") and "Dubblett" in decided[ids[1]] and "passerat" in decided[ids[3]]
    assert (await Items(db).get(rule))["hits"] == 1   # counted, so its owner sees whether it works


def test_a_condition_matches_words_case_insensitively():
    assert matches({"any": ["toronto"]}, "Sales, TORONTO")
    assert not matches({"any": ["toronto"], "none": ["intern"]}, "Intern, Toronto")
    assert matches({"all": ["sommar", "2027"]}, "Sommarjobb 2027")
    assert not matches({}, "allt")   # an empty condition never throws anything away


async def test_researchers_review_shows_each_sources_yield_and_its_rules(db):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from agent import routines
    from agent.due import review

    await routines.put_stream(db, {"id": "lesswrong", "type": "rss", "url": "https://lesswrong.com/feed"}, "system")
    await routines.put_stream(db, {"id": "amanita", "type": "page", "url": "https://amanita-design.net"}, "system")
    await routines.put_stream(db, {"id": "dead-careers", "type": "page", "url": "https://x.example/jobs"}, "system")
    await routines.pause(db, "stream", "dead-careers", "system", "pausad efter 5 fel i rad", health="404")
    for i in range(25):
        await event(db, "stream:lesswrong", "news", title=f"Inlägg {i}")
    eid = await event(db, "stream:amanita", "news", title="Nytt spel")
    await add_candidate(db, "news", "Nytt spel", "b", {}, event_id=eid)
    await Items(db).create("rule", "LessWrong: inga AI-nyheter", "researcher",
                           data={"applies_to": "lesswrong", "condition": json.dumps({"any": ["Hugging Face"]}), "action": "drop", "reason": "Ägaren ville inte"})
    text = await review(db, "researcher", datetime(2026, 10, 21, 12, tzinfo=ZoneInfo("Europe/Stockholm")))
    assert "lesswrong: 25 in, 0 kept, 0 sent · low yield" in text
    assert "amanita: 1 in, 1 kept, 0 sent" in text
    assert "dead-careers: 0 in, 0 kept, 0 sent · paused (404)" in text
    assert "LessWrong: inga AI-nyheter" in text and "hits 0" in text


async def test_postings_with_the_same_title_on_the_same_site_are_not_duplicates(db):
    await add_candidate(db, "opportunity", "Software Engineering Intern 2027, Google", "b", {},
                        url="https://www.google.com/about/careers/jobs/100028133205254854-london")
    ids = [
        await event(db, "research:jobs", "opportunity", title="Software Engineering Intern 2027, Google",
                    url="https://www.google.com/about/careers/jobs/142747733357142726-emea"),        # another posting, same site
        await event(db, "stream:aggregator", "opportunity", title="Software Engineering Intern 2027, Google",
                    url="https://jobs.example/google-intern"),                                          # the same news elsewhere
    ]
    decided = await screen(db, await db.fetchall("SELECT * FROM events ORDER BY id"))
    assert set(decided) == {ids[1]}
