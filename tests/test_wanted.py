import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from agent import routines, wanted
from agent.claude_gate import ClaudeGate, RunResult
from agent.config import load_settings
from agent.db import Database
from agent.items import Items

TZ = ZoneInfo("Europe/Stockholm")
NOW = datetime(2026, 10, 26, 19, 0, tzinfo=TZ)

INVESTIGATED = {
    "interpretation": "Swishs årliga tävling där illustratörer gör kort till appen",
    "names": ["Årets kort", "Swish-vykort"],
    "organizer": "Swish (Getswish AB)",
    "past": [{"date": "2025-11-04", "title": "Swish bjuder in till tävling", "url": "https://mobil.example/swish"}],
    "cycle": {"kind": "yearly", "window_start": "11-03", "window_end": "11-23", "note": "förra omgången 4–23 nov"},
    "channels": [{"kind": "organizer", "name": "swish.nu", "url": "https://swish.example/arets-kort", "why": "arrangörens sida"}],
    "watches": [
        {"type": "page", "name": "Årets kort", "target": "https://swish.example/arets-kort", "lang": "sv", "why": "arrangörens sida"},
        {"type": "topic", "name": "nyheter", "target": "Swish \"Årets kort\"", "lang": "sv", "why": "pressen skriver när den öppnar"},
    ],
    "current": {"found": False},
    "summary": "Heter Årets kort, öppnar i början av november.",
}


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    Items.watchers = [routines.watch]
    await routines.refresh(d)
    yield d
    Items.watchers = []
    await d.close()


def seeker(db, tmp_path, structured=None):
    import subprocess

    from agent.memory import MemoryRepo

    async def runner(task, ws, on_rl):
        return RunResult(text="", structured=structured)

    mem = tmp_path / "memory"
    if not mem.exists():
        mem.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=mem, check=True)
    return wanted.Wanted(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), MemoryRepo(mem))


def test_the_window_of_a_yearly_thing_and_when_to_look_again():
    cycle = {"kind": "yearly", "window_start": "11-03", "window_end": "11-23"}
    assert wanted.window(cycle, date(2026, 10, 26)) == (date(2026, 11, 3), date(2026, 11, 23))
    assert wanted.next_look(cycle, date(2026, 10, 1)) == (date(2026, 10, 27), False)   # a week before it opens
    assert wanted.next_look(cycle, date(2026, 11, 10)) == (date(2026, 11, 13), True)   # inside: every few days
    assert wanted.window(cycle, date(2026, 12, 1)) == (date(2027, 11, 3), date(2027, 11, 23))
    assert wanted.next_look({"kind": "unknown"}, date(2026, 10, 26)) == (date(2026, 11, 9), False)


class Page:
    def __init__(self, status=200, text="", content=b""):
        self.status_code, self.text, self.content, self.url = status, text, content, "https://x"


class Client:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    async def get(self, url):
        self.asked.append(url)
        return next((p for key, p in self.pages.items() if key in url), Page(404))


def rss(*titles):
    items = "".join(f"<item><title>{t}</title><link>https://n/{i}</link></item>" for i, t in enumerate(titles))
    return f"<?xml version='1.0'?><rss><channel><title>x</title>{items}</channel></rss>".encode()


async def test_a_watch_is_checked_against_the_earlier_rounds():
    names = ["Årets kort", "Swish-vykort"]
    past = [{"date": "2025-11-04", "title": "t", "url": "u"}]
    client = Client({
        "swish.example": Page(text="<html><body><h1>Årets kort 2026</h1></body></html>"),
        "news.google.com": Page(content=rss("Swish bjuder in: Årets kort är här")),
        "feed.example": Page(content=rss("Något annat")),
    })
    page = await wanted.backtest({"id": "p", "type": "page", "target": "https://swish.example/k"}, names, past, client)
    assert page["result"] == "pass"
    news = await wanted.backtest({"id": "t", "type": "topic", "target": "Swish \"Årets kort\"", "lang": "sv"}, names, past, client)
    assert news["result"] == "pass" and "2025-11-04" in news["note"]
    assert "after%3A2025-09-20" in client.asked[-1] and "when" not in client.asked[-1]   # around last year's round, not the past week
    assert wanted.mentions("Vinnarna av årets Swish-vykort korade", ["Årets vykort (Swish)"]) == "Årets vykort (Swish)"
    empty = await wanted.backtest({"id": "js", "type": "page", "target": "https://js.example"}, names, past,
                                  Client({"js.example": Page(text="<html><body><script>app()</script></body></html>")}))
    assert empty["result"] == "fail" and "JavaScript" in empty["note"]
    feed = await wanted.backtest({"id": "f", "type": "feed", "target": "https://feed.example"}, names, past, client)
    assert feed["result"] == "unverifiable"   # a feed only shows the latest
    words = await wanted.backtest({"id": "w", "type": "topic", "target": "Swish illustrationstävling", "lang": "sv"}, names, past,
                                  Client({"news.google.com": Page(content=rss("Inget om det"))}))
    assert words["result"] == "fail"   # the owner's own words would never have found it


async def test_an_investigation_sets_up_watches_checked_against_the_past_and_waits_for_the_window(db, tmp_path, monkeypatch):
    from agent import direction_ops as ops

    await ops.add_topic(db, "swish-illustrationstavling", "Swish illustrationstävling", "sv", "Frida vill veta", urgent=True, by="assistant")
    made = await wanted.migrate(db)
    assert len(made) == 1 and await wanted.migrate(db) == []   # once
    w = (await Items(db).find("wanted"))[0]
    assert w["status"] == "investigating" and json.loads(w["watches"]) == ["topic-swish-illustrationstavling"]

    async def checked(watch, names, past, client=None):
        return {"watch": watch["id"], "type": watch["type"], "result": "pass" if watch["type"] == "page" else "fail", "note": "x"}

    monkeypatch.setattr(wanted, "backtest", checked)
    s = seeker(db, tmp_path, INVESTIGATED)
    assert (await s.next_up(NOW))[0]["id"] == w["id"]
    assert await s.tick(NOW)
    w = await Items(db).get(w["id"])
    assert w["status"] == "watching" and w["review_at"] == "2026-10-27"   # a week before the window opens
    assert json.loads(w["names"])[0] == "Årets kort" and len(json.loads(w["checks"])) == 2
    watches = json.loads(w["watches"])
    assert watches == [f"j{w['id']}-arets-kort", f"topic-j{w['id']}-nyheter"]
    assert (await routines.find(db, "stream", "topic-swish-illustrationstavling"))["status"] == "paused"   # her words, replaced
    assert (await routines.find(db, "stream", watches[0]))["_config"]["stream"]["urgent"]
    log = await Items(db).history(w["id"])
    assert "underkända" in log[-1]["note"]   # the failed news search is shown
    assert await s.next_up(NOW) is None   # nothing more today


async def test_what_is_open_now_goes_to_triage_and_an_unclear_request_asks_the_owner(db, tmp_path, monkeypatch):
    async def checked(watch, names, past, client=None):
        return {"watch": watch["id"], "type": watch["type"], "result": "pass", "note": "x"}

    monkeypatch.setattr(wanted, "backtest", checked)
    wid = await wanted.create(db, "Swishtävling", "assistant", interpretation="en tävling hos Swish")
    out = {**INVESTIGATED, "current": {"found": True, "title": "Årets kort 2027", "url": "https://swish.example/arets-kort",
                                       "summary": "Öppen till 23 nov", "kind": "opportunity", "deadline": "2026-11-23",
                                       "organization": "Swish", "checked": "page"},
           "question": {"text": "Menar du Årets kort?", "guess": "Ja, troligen", "examples": ["Årets kort", "Swish Student"]}}
    found = []
    s = seeker(db, tmp_path, out)

    async def on_found():
        found.append(True)

    s.on_found = on_found
    assert await s.tick(NOW) and found
    event = await db.fetchone("SELECT source, payload FROM events WHERE source = ?", (f"wanted:{wid}",))
    assert json.loads(event["payload"])["asked_for"] is True
    w = await Items(db).get(wid)
    assert w["status"] == "waiting" and w["finding"].startswith("Årets kort 2027")
    q = await Items(db).get(int(w["question"]))
    assert q["to"] == "owner" and "Swish Student" in q["context"]
    assert await s.next_up(NOW) is None   # waits for the answer
    await Items(db).update(q["id"], "assistant", status="answered", data={"answer": "Ja, Årets kort"})
    up = await s.next_up(NOW)
    assert up[0]["id"] == wid and "besvarad" in up[1]


async def test_a_quiet_watch_brings_the_request_back(db, tmp_path):
    wid = await wanted.create(db, "x", "assistant")
    await Items(db).update(wid, "researcher", status="watching", review_at="2027-01-01",
                           data={"watches": json.dumps(["topic-x"]), "investigated_at": "2026-10-01T10:00"})
    s = seeker(db, tmp_path)
    assert await s.next_up(NOW) is None
    await db.execute("INSERT INTO stream_stats (stream, polls) VALUES ('topic-x', 97)")
    up = await s.next_up(NOW)
    assert up[0]["id"] == wid and "inte gett något" in up[1]
