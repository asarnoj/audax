import json

import pytest

from agent.db import Database
from agent.inspect import Inspector


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


async def test_health_flags_repeated_claude_failures_only(db):
    i = Inspector(db)
    assert await i.health() == []
    for _ in range(3):
        await db.execute("INSERT INTO claude_runs (kind, is_error, error) VALUES ('dispatcher', 1, 'boom')")
    assert "Claude misslyckas upprepat" in (await i.health())[0]
    await db.execute("INSERT INTO claude_runs (kind, is_error) VALUES ('chat', 0)")
    assert await i.health() == []


async def test_day_and_runs_show_traces_and_skipped(db):
    await db.execute("INSERT INTO messages (telegram_message_id, direction, text, origin) VALUES (1, 'out', 'Hackathon i nov', 'proactive')")
    await db.execute(
        "INSERT INTO research_runs (research_id, findings, returned, skipped) VALUES ('jobs', 0, 2, ?)",
        (json.dumps([{"title": "Goldman Summer Analyst", "reason": "inte intresserad av banker"}]),),
    )
    await db.execute(
        "INSERT INTO claude_runs (kind, model, duration_ms, is_error, trace, output) VALUES ('research', 'sonnet', 24000, 0, ?, '{}')",
        (json.dumps([{"tool": "WebSearch", "input": "summer internship 2027 Stockholm", "error": False}]),),
    )
    i = Inspector(db)
    day = await i.day()
    assert "BRUNO→ Hackathon i nov" in day and "2 fynd, 0 nya, 1 bortvalda" in day and "inte intresserad av banker" in day
    runs = await i.runs("research")
    assert "WebSearch: summer internship 2027 Stockholm" in runs
    assert "Skickat på eget initiativ: 1" in await i.summary()
