import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.claude_gate import ClaudeGate, RunResult
from agent.config import load_settings
from agent.controls import Controls
from agent.db import Database
from agent.pipeline.dispatcher import Dispatcher
from agent.pipeline.outbox import Outbox, add_candidate
from agent.schedule import Schedule

NOW = datetime(2026, 10, 12, 14, 0, tzinfo=ZoneInfo("Europe/Stockholm"))


class Clock(Schedule):
    def now(self):
        return NOW


class FakeMemory:
    def read(self, *names):
        return ""


@pytest.fixture
async def setup(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.open()
    sent = []

    async def send_text(text, candidate_id=None, origin=None):
        sent.append(text)
        return [await db.execute("INSERT INTO messages (telegram_message_id, direction, text, origin, candidate_id) VALUES (1, 'out', ?, 'proactive', ?)", (text, candidate_id))]

    async def send_poll(q, o, multiple=True):
        return "p", None

    async def runner(task, ws, on_rl):
        return RunResult(text="")

    schedule = Clock(load_settings(), db, [])
    outbox = Outbox(db, schedule, send_text)
    d = Dispatcher(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), FakeMemory(), schedule, outbox, send_poll)
    notes = []

    async def research(rid):
        return 3

    async def notify(text):
        notes.append(text)

    yield db, Controls(db, d, research, notify), outbox, sent, notes
    await db.close()


async def test_pause_and_resume(setup):
    db, c, *_ = setup
    assert await c.pause("quiet", 120) == "Tyst läge till 16:00."
    assert await c.dispatcher.silenced_until() is not None
    assert await c.pause("quiet", 0) == "Tyst läge avslutat."
    assert await c.dispatcher.silenced_until() is None
    assert [r["value"] for r in await db.fetchall("SELECT value FROM feedback")] == ["quiet:120m", "quiet:av"]


async def test_shelf_and_why(setup):
    db, c, outbox, sent, _ = setup
    cid = await add_candidate(db, "event", "Hackathon", "Hackathon på Uni", {"relevance": "high"}, url="https://h")
    await outbox.set_tier(cid, "shelf", "shelved")
    text, rows = await c.shelf()
    assert "Hackathon på Uni" in text and "https://h" in text and len(rows) == 1
    assert (await c.shelf())[0] == "Hyllan är tom."

    cid2 = await add_candidate(db, "opportunity", "Jobb", "Sommarjobb", {"relevance": "high", "reason": "passar"})
    await outbox.record(cid2, "dispatcher@1", "next_gap (nu)", "lucka efter labb", {"availability": "ledig nu"})
    await outbox.deliver([await db.fetchone("SELECT * FROM candidates WHERE id = ?", (cid2,))])
    why = await c.why(await c.last_proactive_message_id())
    assert "lucka efter labb" in why and "relevance=high" in why and "Läge då: ledig nu" in why


async def test_research_runs_in_background_and_notifies(setup):
    _, c, _, _, notes = setup
    assert "Finns ingen" in await c.start_research("nope")
    rid = c.research_ids()[0]
    assert "Startade" in await c.start_research(rid)
    await asyncio.sleep(0.05)
    assert notes == [f"{rid}: klar, 3 nya fynd. Det som inte skickades ligger på hyllan."]
