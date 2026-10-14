import asyncio
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, RunResult
from agent.db import Database
from agent.office import Office
from agent.posts import Posts
from agent.site import build_app, linked


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


def memory(tmp_path: Path) -> Path:
    mem = tmp_path / "memory"
    (mem / "arbetsminne").mkdir(parents=True)
    (mem / "arbetsminne" / "director.md").write_text("Fokus: sommarjobb #S1")
    (mem / ".git").mkdir()
    (mem / ".git" / "config").write_text("secret")
    return mem


async def test_the_office_shows_a_run_in_progress_with_its_steps(db, tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def runner(task, ws, on_rl):
        task.trace.append({"tool": "web_read", "input": "https://example.org", "error": False})
        started.set()
        await release.wait()
        return RunResult(text="ok")

    gate = ClaudeGate(db, tmp_path, runner=runner)
    office = Office(db, memory(tmp_path), "Europe/Stockholm", status=gate.status)
    run = asyncio.create_task(gate.run(ClaudeTask(kind="research", prompt="x", priority=Priority.RESEARCH)))
    await started.wait()
    now = await office.now()
    assert now["roles"]["researcher"]["working"][0]["task"] == "ett sökuppdrag"
    assert now["roles"]["researcher"]["working"][0]["steps"] == ["web_read: https://example.org"]
    assert now["roles"]["director"]["working"] == []
    release.set()
    await run
    now = await office.now()
    assert now["roles"]["researcher"]["working"] == [] and now["roles"]["researcher"]["last_run"]["task"] == "ett sökuppdrag"


async def test_a_case_gathers_its_posts_messages_and_bookings(db, tmp_path):
    office = Office(db, memory(tmp_path), "Europe/Stockholm")
    await db.execute("INSERT INTO pursuits (title, organization, status) VALUES ('Sommarpraktik', 'Spotify', 'interested')")
    await Posts(db).send("director", "researcher", "order", "Leta liknande som #S1")
    mid = await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (1, 'out', 'Spotify har öppnat')")
    await db.execute("INSERT INTO message_refs (message_id, ref) VALUES (?, '#S1')", (mid,))
    case = await office.case("S1")
    assert case["ref"] == "#S1" and "Spotify" in case["about"]
    assert [p["ref"] for p in case["posts"]] == ["#P1"]
    assert case["messages"][0]["text"] == "Spotify har öppnat"
    timeline = await office.timeline(10, "researcher")
    assert [ev["type"] for ev in timeline] == ["post"]  # the conversation is the Assistant's, not Researcher's


async def test_the_pages_render_and_the_archive_stays_inside_its_folders(db, tmp_path):
    mem = memory(tmp_path)
    app = build_app(db, Office(db, mem, "Europe/Stockholm"), {"minne": mem})
    client = TestClient(app)
    for url in ("/", "/fragment/nu", "/roll/director", "/arende/", "/arende/?q=sommar", "/arkiv/", "/arkiv/minne/", "/tabeller/",
                "/tabeller/posts", "/api/nu"):
        assert client.get(url).status_code == 200, url
    director = client.get("/roll/director").text
    assert "Fokus: sommarjobb" in director and '<a href="/arende/S1">#S1</a>' in director
    assert "Fokus" in client.get("/arkiv/minne/arbetsminne/director.md").text
    assert "secret" not in client.get("/arkiv/minne/.git/config").text
    assert "secret" not in client.get("/arkiv/minne/../memory/.git/config").text
    assert "Ingen sådan tabell" in client.get("/tabeller/kv; DROP TABLE posts").text


def test_only_real_ids_become_links():
    assert linked("Lyssna på MP3 och se #F12") == 'Lyssna på MP3 och se <a href="/arende/F12">#F12</a>'
    assert linked("<script>") == "&lt;script&gt;"
