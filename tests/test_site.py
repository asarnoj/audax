from pathlib import Path

import pytest

from agent.items import Items
from starlette.testclient import TestClient

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


async def test_a_case_gathers_its_posts_messages_and_bookings(db, tmp_path):
    office = Office(db, memory(tmp_path), "Europe/Stockholm")
    await Items(db).create("aim", "Sommarpraktik", "director", kind="pursuit", data={"organization": "Spotify"}, source={"type": "inferred"})
    await Posts(db).send("director", "researcher", "order", "Leta liknande som #S1")
    mid = await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (1, 'out', 'Spotify har öppnat')")
    await db.execute("INSERT INTO message_refs (message_id, ref) VALUES (?, '#S1')", (mid,))
    case = await office.case("S1")
    assert case["ref"] == "#S1" and "Spotify" in case["about"]
    assert [p["ref"] for p in case["posts"]] == ["#P1"]
    assert case["messages"][0]["text"] == "Spotify har öppnat"


async def test_the_pages_render_and_the_files_stay_inside_their_folders(db, tmp_path):
    mem = memory(tmp_path)
    await db.execute("INSERT INTO claude_runs (kind, output, finished_at) VALUES ('triage', 'Släppte #F3', datetime('now'))")
    client = TestClient(build_app(db, Office(db, mem, "Europe/Stockholm"), {"memory": mem}))
    goal = await Items(db).create("aim", "Sommarjobb 2027", "director", kind="goal", data={"why": "Komma in i branschen"})
    await Items(db).create("step", "Armada-listan", "director", owner="researcher", parent=goal, review_at="2026-11-01",
                           data={"action": "research", "done_when": "Bolagen med sommarpraktik"})
    assert "Armada-listan" in client.get("/data/items/").text and "Rymmer: <a href=\"/data/item/2\">#T2</a>" in client.get(f"/data/item/{goal}").text
    for url in ("/", "/data/", "/data/run/1", "/data/items/?form=step", "/data/case/", "/data/case/?q=sommar", "/data/files/", "/data/files/memory/",
                "/data/tables/", "/data/tables/posts"):
        assert client.get(url).status_code == 200, url
    assert '<a href="/data/case/F3">#F3</a>' in client.get("/data/run/1").text
    assert "Fokus" in client.get("/data/files/memory/arbetsminne/director.md").text
    assert "secret" not in client.get("/data/files/memory/.git/config").text
    assert "secret" not in client.get("/data/files/memory/../memory/.git/config").text
    assert "No such table" in client.get("/data/tables/kv; DROP TABLE posts").text


def test_only_real_ids_become_links():
    assert linked("Lyssna på MP3 och se #F12, #R4") == 'Lyssna på MP3 och se <a href="/data/case/F12">#F12</a>, <a href="/data/run/4">#R4</a>'
    assert linked("<script>") == "&lt;script&gt;"
