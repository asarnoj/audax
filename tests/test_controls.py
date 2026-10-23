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
    assert len((await c.shelf())[1]) == 1  # looking changes nothing
    assert (await c.shelf("praktik"))[1] == rows  # no match: everything, rather than nothing
    assert await outbox.mark_pulled([cid]) == 1  # told the owner: off the shelf, once
    assert await outbox.mark_pulled([cid]) == 0
    assert (await c.shelf())[0] == "[internal] Nothing saved for later."

    cid2 = await add_candidate(db, "opportunity", "Jobb", "Sommarjobb", {"relevance": "high", "reason": "passar"})
    await outbox.record(cid2, "dispatcher@1", "next_gap (nu)", "lucka efter labb", {"availability": "ledig nu"})
    await outbox.deliver([await db.fetchone("SELECT * FROM candidates WHERE id = ?", (cid2,))])
    why = await c.why(await c.last_proactive_message_id())
    assert "lucka efter labb" in why and "relevance=high" in why and "Läge då: ledig nu" in why


async def test_research_runs_in_background_and_notifies(setup):
    _, c, _, _, notes = setup
    assert "Finns ingen" in await c.start_research("nope")
    rid = c.research_ids()[0]
    assert "Started" in await c.start_research(rid)
    await asyncio.sleep(0.05)
    assert notes == ["Sökningen du bad om är klar."]  # no chat: a plain line, never internal names

    spoken = []

    async def speak(instruction, about=frozenset()):
        spoken.append(instruction)
        return [1]

    c.speak = speak
    await c.start_research(rid)
    await asyncio.sleep(0.05)
    assert "3 new finding" in spoken[0] and len(notes) == 1  # through the voice, no plain line



async def test_recent_items_explain_what_happened(setup):
    db, c, outbox, *_ = setup
    eid = await db.execute("INSERT INTO events (source, kind, dedupe_key, payload) VALUES ('stream:topic-machinarium-2', 'news', 'a', '{\"title\": \"Machinarium 2 trailer\"}')")
    cid = await db.execute("INSERT INTO candidates (event_id, kind, title, body, status, features) VALUES (?, 'news', 't', 'b', 'dropped', '{\"reason\": \"bara en recension\"}')", (eid,))
    await outbox.record(cid, "dispatcher@1", "drop", "inget nytt", {})
    text = await c.recent_items("machinarium")
    assert "Machinarium 2 trailer → left out: inget nytt" in text
    assert "Nothing has come in" in await c.recent_items("nope")


async def test_a_promise_that_waits_for_something_is_kept_when_it_is_done(setup, monkeypatch):
    from agent.due import Calendar, follower
    from agent.items import Items
    from agent.tools import TurnContext, _control_tools

    db, c, *_ = setup
    monkeypatch.setattr(Items, "watchers", [follower("Europe/Stockholm")])
    tools = {t.name: t for t in _control_tools(c, TurnContext())}
    plan = await Items(db).create("step", "Kolla att Collide går att gå på som individ", "director", owner="researcher",
                                  review_at="2030-01-01", data={"action": "research", "done_when": "Svar med länk"})
    out = (await tools["book"].handler({"kind": "promise", "what": "Berätta vad Researcher kom fram till om Collide",
                                        "after": f"#T{plan}", "when": "2030-02-01 12:00"}))["content"][0]["text"]
    assert "Booked #T" in out and "Promise only that" in out
    promise = (await Items(db).find("step", owner="assistant"))[0]
    assert promise["after"] == f"#T{plan}" and promise["review_at"] == "2030-02-01 12:00"

    now = (await tools["situation"].handler({}))["content"][0]["text"]
    assert "Collide" in now and "Researcher" in now and f"efter #T{plan}" in now

    said = []

    async def speak(instruction, about=frozenset()):
        said.append(instruction)
        return [1]

    calendar = Calendar(db, "Europe/Stockholm", wake=lambda role, why: None, speak=speak)
    await calendar.tick()
    assert said == []   # not yet: what it waits for isn't done
    await Items(db).update(plan, "researcher", status="done", data={"result": "Ja, öppen för individer"}, note="klart")
    await calendar.tick()
    assert len(said) == 1 and "now done" in said[0] and f"#T{plan}" in said[0]
    assert (await Items(db).get(promise["id"]))["status"] == "done"


async def test_book_moves_and_cancels_through_one_tool(setup):
    from agent.items import Items
    from agent.tools import TurnContext, _control_tools

    db, c, *_ = setup
    tools = {t.name: t for t in _control_tools(c, TurnContext())}
    target = await Items(db).create("aim", "Medgrundare före jul", "director", kind="goal")
    await tools["book"].handler({"kind": "promise", "what": "Hör av mig om planen", "after": f"#S{target}"})
    promise = (await Items(db).find("step", owner="assistant"))[0]
    out = (await tools["book"].handler({"id": f"#T{promise['id']}", "cancel": True}))["content"][0]["text"]
    assert "cancelled" in out and (await Items(db).get(promise["id"]))["status"] == "dropped"


async def test_the_situation_shows_the_nights_work_apart_from_the_latest_fast_skill(setup):
    from agent.situation import situation

    db, *_ = setup
    await db.execute("INSERT INTO claude_runs (kind, finished_at, summary) VALUES ('director_understand', '2026-10-22 23:58:00', 'Lade steg för Google.')")
    await db.execute("INSERT INTO claude_runs (kind, finished_at, is_error) VALUES ('triage', '2026-10-23 07:50:00', 1)")
    text = await situation(db, "Europe/Stockholm")
    line = next(x for x in text.splitlines() if x.startswith("- Director"))
    assert "daily review" in line.lower() and "Lade steg för Google." in line and "triage" in line and "misslyckades" in line


def test_repeated_claude_failures_reach_the_owner_in_their_own_words():
    from agent.inspect import claude_failing

    raw = "ResultError: Claude Code returned an error result: Failed to authenticate: OAuth session expired and could not be refreshed"
    assert claude_failing(raw).startswith("Claude-inloggningen på Pi:n har gått ut") and "ResultError" not in claude_failing(raw)
    assert "Error" not in claude_failing("ResultError: boom")
