import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent.claude_gate import ClaudeGate, RunResult
from agent.config import load_settings
from agent.db import Database
from agent.pipeline.dispatcher import FOCUS_KEY, Dispatcher
from agent.pipeline.outbox import Outbox, add_candidate
from agent.pipeline.triage import Triage
from agent.research import ResearchTask, Researcher, normalize_url
from agent.schedule import Schedule
from agent.telegram_bot import _parse_duration

TZ = ZoneInfo("Europe/Stockholm")
NOW = datetime(2026, 10, 12, 14, 0, tzinfo=TZ)


class FakeMemory:
    def read(self, *names):
        return "(minne)"


class Clock(Schedule):
    def now(self):
        return NOW


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


def gate_returning(db, tmp_path, structured, calls=None):
    async def runner(task, ws, on_rl):
        if calls is not None:
            calls.append(task)
        return RunResult(text="", structured=structured(task) if callable(structured) else structured)

    return ClaudeGate(db, tmp_path, runner=runner)


async def make_dispatcher(db, tmp_path, structured, calls=None):
    sent = []

    async def send_text(text, candidate_id=None, origin=None):
        sent.append(text)
        return [await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (1, 'out', ?)", (text,))]

    async def send_poll(q, opts):
        return "p", None

    schedule = Clock(load_settings(), db, [])
    outbox = Outbox(db, schedule, send_text)
    d = Dispatcher(load_settings(), db, gate_returning(db, tmp_path, structured, calls), FakeMemory(), schedule, outbox, send_poll)
    return d, sent


# --- dispatcher -------------------------------------------------------------


async def test_dispatcher_applies_tiers_and_bundles(db, tmp_path):
    a = await add_candidate(db, "event", "Hackathon", "Hackathon på Uni", {"time_sensitivity": "medium"}, url="https://x/a")
    b = await add_candidate(db, "opportunity", "Jobb", "Sommarjobb", {"time_sensitivity": "medium"}, url="https://x/b")
    c = await add_candidate(db, "news", "Nyhet", "Kul nyhet", {})
    e = await add_candidate(db, "event", "Gammalt", "Redan över", {})
    decisions = {
        "decisions": [
            {"candidate_id": a, "tier": "bundle", "send_now": True, "reason": "lucka nu"},
            {"candidate_id": b, "tier": "bundle", "send_now": True, "reason": "lucka nu"},
            {"candidate_id": c, "tier": "shelf", "send_now": False, "reason": "inte värt att störa"},
            {"candidate_id": e, "tier": "drop", "send_now": False, "reason": "utgånget"},
        ],
    }
    d, sent = await make_dispatcher(db, tmp_path, decisions)
    await d.run()

    assert len(sent) == 1 and "https://x/a" in sent[0] and "https://x/b" in sent[0]  # one message, both items
    status = {r["id"]: r["status"] for r in await db.fetchall("SELECT id, status FROM candidates")}
    assert status == {a: "sent", b: "sent", c: "shelved", e: "dropped"}
    links = await db.fetchall("SELECT candidate_id FROM message_candidates")
    assert {r["candidate_id"] for r in links} == {a, b}  # both traceable for /varfor and reactions
    assert (await db.fetchone("SELECT count(*) AS n FROM decisions"))["n"] == 4

    rows = await d.outbox.pull_shelf()
    assert [r["id"] for r in rows] == [c]
    assert await d.outbox.pull_shelf() == []  # pulled items leave the shelf


async def test_dispatcher_only_reruns_when_state_changes(db, tmp_path):
    calls = []
    a = await add_candidate(db, "event", "A", "A", {})
    d, _ = await make_dispatcher(db, tmp_path, lambda t: {"decisions": [{"candidate_id": a, "tier": "next_gap", "send_now": False, "reason": "vänta"}]}, calls)
    await d.run()
    await d.run()
    assert len(calls) == 1  # unchanged outbox: no second Claude run
    await add_candidate(db, "event", "B", "B", {})
    await d.run()
    assert len(calls) == 2


async def test_focus_blocks_everything_but_interrupts(db, tmp_path):
    a = await add_candidate(db, "event", "A", "inte akut", {})
    b = await add_candidate(db, "schedule_change", "B", "akut", {"time_sensitivity": "high"})
    d, sent = await make_dispatcher(
        db,
        tmp_path,
        {"decisions": [
            {"candidate_id": a, "tier": "next_gap", "send_now": True, "reason": "x"},
            {"candidate_id": b, "tier": "interrupt", "send_now": True, "reason": "y"},
        ]},
    )
    await d.silence(FOCUS_KEY, NOW + timedelta(hours=1))
    await d.run()
    assert sent == ["akut"]


async def test_dispatcher_falls_back_when_claude_fails(db, tmp_path):
    await add_candidate(db, "event", "A", "inte akut", {"time_sensitivity": "medium"})
    await add_candidate(db, "schedule_change", "B", "akut", {"time_sensitivity": "high"})
    d, sent = await make_dispatcher(db, tmp_path, None)  # structured=None -> treated as failure
    await d.run()
    assert sent == ["akut"]
    assert (await db.fetchone("SELECT module FROM decisions"))["module"] == "fallback@1"


# --- triage -----------------------------------------------------------------


async def test_triage_creates_candidates_and_drops(db, tmp_path):
    e1 = await db.execute("INSERT INTO events (source, kind, dedupe_key, payload) VALUES ('research:x', 'event', 'a', ?)",
                          (json.dumps({"title": "Bra", "url": "https://a", "date": "2026-10-20"}),))
    e2 = await db.execute("INSERT INTO events (source, kind, dedupe_key, payload) VALUES ('research:x', 'event', 'b', ?)",
                          (json.dumps({"title": "Dåligt", "url": "https://b"}),))
    lv = dict(relevance="high", actionability="high", time_sensitivity="medium", novelty="high", confidence="high", complement="high")
    out = {"items": [
        {"event_id": e1, "keep": True, "brief": "Kolla in det här", "reason": "passar", **lv},
        {"event_id": e2, "keep": False, "brief": "-", "reason": "irrelevant", **lv},
    ]}
    t = Triage(load_settings(), db, gate_returning(db, tmp_path, out), FakeMemory())
    assert await t.run() == 1
    rows = {r["title"]: r for r in await db.fetchall("SELECT * FROM candidates")}
    assert rows["Bra"]["status"] == "pending" and rows["Bra"]["expires_at"].startswith("2026-10-20T21:59")
    assert rows["Dåligt"]["status"] == "dropped"
    assert json.loads(rows["Bra"]["features"])["relevance"] == "high"
    assert await t.run() == 0  # events are marked triaged


# --- research ---------------------------------------------------------------


async def test_research_stores_new_findings_and_dedupes(db, tmp_path):
    finding = {"title": "Hack", "url": "https://Event.se/hack/?utm_source=x", "summary": "s", "why_you": "w", "kind": "event"}
    r = Researcher(load_settings(), db, gate_returning(db, tmp_path, {"findings": [finding]}), FakeMemory())
    task = ResearchTask("events", "mål", "Sthlm", 4, True)
    assert await r.run(task) == 1
    assert await r.run(task) == 0  # same URL again
    assert normalize_url("https://Event.se/hack/?utm_source=x") == "https://event.se/hack"


def test_parse_duration():
    assert _parse_duration("2h") == timedelta(hours=2)
    assert _parse_duration("30m") == timedelta(minutes=30)
    assert _parse_duration("1h30m") == timedelta(hours=1, minutes=30)
    assert _parse_duration("imorgon") is None


async def test_delivery_goes_through_the_voice_and_falls_back(db, tmp_path):
    a = await add_candidate(db, "event", "Hackathon", "Hackathon på Uni", {}, url="https://h")
    d, sent = await make_dispatcher(db, tmp_path, {"decisions": [{"candidate_id": a, "tier": "next_gap", "send_now": True, "reason": "lucka"}]})
    spoken = []

    async def speak(instruction):
        spoken.append(instruction)
        return [await db.execute("INSERT INTO messages (telegram_message_id, direction, text, origin, in_session) VALUES (2, 'out', 'Kolla Uni-hacket', 'proactive', 1)")]

    d.outbox.speak = speak
    await d.run()
    assert sent == [] and "Hackathon på Uni" in spoken[0] and "https://h" in spoken[0]
    assert (await db.fetchone("SELECT candidate_id FROM message_candidates"))["candidate_id"] == a

    b = await add_candidate(db, "event", "B", "Utkast B", {})
    d2, sent2 = await make_dispatcher(db, tmp_path, {"decisions": [{"candidate_id": b, "tier": "next_gap", "send_now": True, "reason": "x"}]})

    async def broken(instruction):
        raise RuntimeError("quota")

    d2.outbox.speak = broken
    await d2.run()
    assert sent2 == ["Utkast B"]  # the draft goes out as-is


# --- robustness: at most once, whatever runs concurrently (duplicates on 12 Oct) -------------------------


async def test_concurrent_dispatcher_runs_deliver_a_candidate_once(db, tmp_path):
    import asyncio

    a = await add_candidate(db, "news", "Machinarium 2", "Trailer ute", {}, url="https://m")
    d, sent = await make_dispatcher(db, tmp_path, {"decisions": [{"candidate_id": a, "tier": "interrupt", "send_now": True, "reason": "nu"}]})
    await asyncio.gather(d.run(force=True), d.run(force=True), d.run(force=True))
    assert len(sent) == 1
    assert (await db.fetchone("SELECT status FROM candidates WHERE id = ?", (a,)))["status"] == "sent"


async def test_delivering_the_same_row_twice_sends_once(db, tmp_path):
    a = await add_candidate(db, "news", "X", "Y", {})
    d, sent = await make_dispatcher(db, tmp_path, {"decisions": []})
    row = await db.fetchone("SELECT * FROM candidates WHERE id = ?", (a,))
    await d.outbox.deliver([row])
    await d.outbox.deliver([row])  # a stale copy from a second, overlapping run
    assert len(sent) == 1


async def test_chat_can_decline_a_delivery_it_already_made(db, tmp_path):
    from agent.pipeline.outbox import AlreadySaid

    a = await add_candidate(db, "news", "X", "Y", {})
    d, sent = await make_dispatcher(db, tmp_path, {"decisions": []})

    async def speak(instruction):
        assert "SKIP" in instruction
        raise AlreadySaid()

    d.outbox.speak = speak
    await d.outbox.deliver([await db.fetchone("SELECT * FROM candidates WHERE id = ?", (a,))])
    assert sent == []  # no fallback draft either
    assert (await db.fetchone("SELECT status FROM candidates WHERE id = ?", (a,)))["status"] == "dropped"


async def test_breaker_holds_delivery_when_something_loops(db, tmp_path):
    from agent.pipeline.outbox import BREAKER_PER_HOUR

    for _ in range(BREAKER_PER_HOUR):
        await db.execute("INSERT INTO messages (telegram_message_id, direction, text, origin) VALUES (1, 'out', 'x', 'proactive')")
    a = await add_candidate(db, "news", "X", "Y", {})
    d, sent = await make_dispatcher(db, tmp_path, {"decisions": []})
    await d.outbox.deliver([await db.fetchone("SELECT * FROM candidates WHERE id = ?", (a,))])
    assert sent == [] and (await db.fetchone("SELECT status FROM candidates WHERE id = ?", (a,)))["status"] == "pending"
    assert await db.kv_get("outbox.breaker")


async def test_failed_delivery_releases_and_crash_recovery(db, tmp_path):
    a = await add_candidate(db, "news", "X", "Y", {})
    d, _ = await make_dispatcher(db, tmp_path, {"decisions": []})

    async def down(text, candidate_id=None, origin=None):
        raise RuntimeError("telegram down")

    d.outbox.send_text = down
    await d.outbox.deliver([await db.fetchone("SELECT * FROM candidates WHERE id = ?", (a,))])
    assert (await db.fetchone("SELECT status FROM candidates WHERE id = ?", (a,)))["status"] == "pending"

    b = await add_candidate(db, "news", "B", "B", {})
    await db.execute("UPDATE candidates SET status = 'sending' WHERE id IN (?, ?)", (a, b))
    mid = await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (1, 'out', 'b')")
    await db.execute("INSERT INTO message_candidates (message_id, candidate_id) VALUES (?, ?)", (mid, b))
    await d.outbox.recover()
    status = {r["id"]: r["status"] for r in await db.fetchall("SELECT id, status FROM candidates")}
    assert status == {a: "pending", b: "sent"}


async def test_concurrent_triage_makes_one_candidate_per_event(db, tmp_path):
    import asyncio

    e = await db.execute("INSERT INTO events (source, kind, dedupe_key, payload) VALUES ('stream:x', 'news', 'k', ?)",
                         (json.dumps({"title": "Ideate", "url": "https://i"}),))
    lv = dict(relevance="high", actionability="high", time_sensitivity="medium", novelty="high", confidence="high", complement="high")
    t = Triage(load_settings(), db, gate_returning(db, tmp_path, {"items": [{"event_id": e, "keep": True, "brief": "b", "reason": "r", **lv}]}), FakeMemory())
    await asyncio.gather(t.run(), t.run())
    assert (await db.fetchone("SELECT count(*) AS n FROM candidates WHERE event_id = ?", (e,)))["n"] == 1


async def test_triage_sees_what_was_already_sent(db, tmp_path):
    calls = []
    sent = await add_candidate(db, "news", "Machinarium 2 trailer", "b", {}, url="https://yt/x")
    await db.execute("UPDATE candidates SET status = 'sent' WHERE id = ?", (sent,))
    await db.execute("INSERT INTO events (source, kind, dedupe_key, payload) VALUES ('stream:y', 'news', 'k2', ?)", (json.dumps({"title": "Trailer"}),))
    t = Triage(load_settings(), db, gate_returning(db, tmp_path, {"items": []}, calls), FakeMemory())
    await t.run()
    assert "Machinarium 2 trailer" in calls[0].prompt and "https://yt/x" in calls[0].prompt
