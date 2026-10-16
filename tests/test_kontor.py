import asyncio
import subprocess

import pytest
from starlette.testclient import TestClient

from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, RunResult
from agent.db import Database
from agent.kontor import Live
from agent.office import Office
from agent.site import build_app


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


def memory(tmp_path):
    mem = tmp_path / "memory"
    (mem / "arbetsminne").mkdir(parents=True)
    git = ["git", "-C", str(mem), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (mem / "profile.md").write_text("Julius")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    return mem, git


async def test_the_stream_tells_what_happened_in_the_office(db, tmp_path):
    mem, git = memory(tmp_path)
    live = Live(db, mem, "Europe/Stockholm")
    c = await live.cursor()
    assert await live.changes(c) == []

    await db.execute("INSERT INTO events (source, kind, payload) VALUES ('stream:hn', 'item', '{}')")
    kept = await db.execute("INSERT INTO candidates (kind, title, body) VALUES ('job', 'Sommarjobb', '')")
    dropped = await db.execute("INSERT INTO candidates (kind, title, body, status, tier) VALUES ('news', 'Brus', '', 'dropped', 'drop')")
    await db.execute("INSERT INTO decisions (candidate_id, module, tier, reason) VALUES (?, 'triage@1', 'drop', 'brus')", (dropped,))
    await db.execute("INSERT INTO decisions (candidate_id, module, tier, reason) VALUES (?, 'dispatcher@2', 'sparad till senare', '')", (kept,))
    await db.execute("INSERT INTO posts (sender, recipient, kind, body) VALUES ('director', 'researcher', 'order', 'Leta')")
    await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (1, 'in', 'hej')")
    (mem / "profile.md").write_text("Julius, KTH")
    subprocess.run([*git, "commit", "-qam", "chat 2026-10-16 10:00"], check=True)

    happened = await live.changes(c)
    types = [h["type"] for h in happened]
    assert {"print", "drop", "pass", "place", "post", "message", "archive"} <= set(types)
    assert next(h for h in happened if h["type"] == "place") == {"type": "place", "to": "board", "ref": f"#F{kept}"}
    assert next(h for h in happened if h["type"] == "archive")["role"] == "assistant"
    assert await live.changes(c) == []  # each thing is told once


async def test_runs_starting_and_going_out_of_the_office(db, tmp_path):
    mem, _ = memory(tmp_path)
    live = Live(db, mem, "Europe/Stockholm")
    started, release = asyncio.Event(), asyncio.Event()

    async def runner(task, ws, on_rl):
        started.set()
        await asyncio.sleep(0)
        task.trace.append({"tool": "mail_search", "input": "", "error": False})
        await release.wait()
        return RunResult(text="ok")

    c = await live.cursor()
    gate = ClaudeGate(db, tmp_path, runner=runner)
    run = asyncio.create_task(gate.run(ClaudeTask(kind="director_understand", prompt="x", priority=Priority.MANAGER)))
    await started.wait()
    await asyncio.sleep(0.01)
    assert [h["type"] for h in await live.changes(c)] == ["start", "out"]
    assert (await live.state())["working"]["director"]["out"] is True
    release.set()
    await run
    assert await live.changes(c) == [{"type": "end", "role": "director", "kind": "director_understand"}]


async def test_what_waits_and_what_a_click_shows(db, tmp_path):
    mem, _ = memory(tmp_path)
    await db.execute("INSERT INTO events (source, kind, payload) VALUES ('stream:hn', 'item', '{}')")
    await db.execute("INSERT INTO candidates (kind, title, body, status) VALUES ('job', 'Sparat', '', 'shelved')")
    live = Live(db, mem, "Europe/Stockholm")
    state = await live.state()
    assert state["waiting"]["director"] == 1 and state["board"][0]["title"] == "Sparat"
    client = TestClient(build_app(db, Office(db, mem, "Europe/Stockholm"), {"minne": mem}))
    assert client.get("/api/kontor/lage").json()["waiting"]["director"] == 1
    for name in ("desk-manager", "archive", "bin", "calendar", "board"):
        assert client.get(f"/api/kontor/objekt/{name}").status_code == 200, name
    assert client.get("/api/kontor/objekt/nothing").status_code == 404
    desk = client.get("/api/kontor/objekt/desk-director").json()
    assert desk["inbox"][0]["what"] == "new" and desk["about"].startswith("Works out")


async def test_a_piece_of_work_shows_what_it_was_about_and_what_it_changed(db, tmp_path):
    mem, git = memory(tmp_path)
    await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (1, 'in', 'Vad har du hittat?')")
    (mem / "profile.md").write_text("Julius, KTH, gillar hackathons")
    subprocess.run([*git, "commit", "-qam", "chat 2026-10-16 10:00"], check=True)
    rid = await db.execute("INSERT INTO claude_runs (kind, duration_ms, output, trace) VALUES ('chat', 4000, 'Två saker.', ?)",
                           ('[{"tool": "get_shelf", "input": "", "error": false}]',))
    w = await Live(db, mem, "Europe/Stockholm").work(rid)
    assert w["task"] == "Conversation" and w["about"][0]["text"] == "Vad har du hittat?"
    assert w["tools"][0]["tool"] == "Looked at the board" and "gillar hackathons" in w["edits"][0]["patch"]
    assert w["edits"][0]["file"] == "profile.md" and w["edits"][0]["added"] == 1
