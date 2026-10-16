import asyncio
import subprocess

import pytest
from starlette.testclient import TestClient

from agent import trail
from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, RunResult
from agent.db import Database
from agent.memory import MemoryRepo
from agent.office import Office
from agent.posts import Posts
from agent.site import build_app


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    trail.attach(d)
    yield d
    trail.attach(None)
    await d.close()


def memory(tmp_path):
    mem = tmp_path / "memory"
    (mem / "arbetsminne").mkdir(parents=True)
    subprocess.run(["git", "-C", str(mem), "init", "-q"], check=True)
    (mem / "profile.md").write_text("Julius")
    subprocess.run(["git", "-C", str(mem), "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(mem), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], check=True)
    return mem


async def test_the_office_follows_its_event_log_from_a_point(db, tmp_path):
    office = Office(db, memory(tmp_path), "Europe/Stockholm")
    state = await office.state()
    assert await office.since(state["cursor"]) == []
    await Posts(db).send("director", "researcher", "order", "Leta")
    await trail.emit("print", "director", n=3)
    happened = await office.since(state["cursor"])
    assert [(h["type"], h["role"]) for h in happened] == [("post", "director"), ("print", "director")]
    assert happened[0]["to"] == "researcher" and happened[0]["what"] == "order"
    assert await office.since(happened[-1]["id"]) == []   # each thing is told once
    assert (await office.counts())["waiting"]["researcher"] == 1


async def test_a_run_starting_going_out_and_ending(db, tmp_path):
    office = Office(db, memory(tmp_path), "Europe/Stockholm")
    started, release = asyncio.Event(), asyncio.Event()

    async def runner(task, ws, on_rl):
        task.trace.append({"tool": "agent__mail_search", "input": "", "error": False})
        started.set()
        await release.wait()
        return RunResult(text="ok")

    c = await office.cursor()
    run = asyncio.create_task(ClaudeGate(db, tmp_path, runner=runner).run(ClaudeTask(kind="director_understand", prompt="x", priority=Priority.MANAGER)))
    await started.wait()
    assert (await office.state())["working"]["director"]["out"] is True
    release.set()
    await run
    assert [h["type"] for h in await office.since(c)] == ["start", "end"]   # this fake runner bypasses the SDK's "out"
    assert (await office.state())["last"]["director"]


async def test_what_waits_and_what_a_click_shows(db, tmp_path):
    mem = memory(tmp_path)
    await db.execute("INSERT INTO events (source, kind, payload) VALUES ('stream:hn', 'item', '{}')")
    await db.execute("INSERT INTO candidates (kind, title, body, status) VALUES ('job', 'Sparat', '', 'shelved')")
    client = TestClient(build_app(db, Office(db, mem, "Europe/Stockholm"), {"memory": mem}))
    state = client.get("/api/state").json()
    assert state["waiting"]["director"] == 1 and state["board"] == 1 and state["boardItems"][0]["title"] == "Sparat"
    for name in ("desk-manager", "archive", "bin", "calendar", "board"):
        assert client.get(f"/api/thing/{name}").status_code == 200, name
    assert client.get("/api/thing/nothing").status_code == 404
    desk = client.get("/api/thing/desk-director").json()
    assert desk["inbox"][0]["what"] == "new" and desk["about"].startswith("Works out")


async def test_a_piece_of_work_is_found_by_its_id(db, tmp_path):
    mem = memory(tmp_path)
    office = Office(db, mem, "Europe/Stockholm")
    await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (1, 'in', 'Vad har du hittat?')")

    async def runner(task, ws, on_rl):
        task.trace.append({"tool": "agent__get_shelf", "input": "", "error": False})
        await Posts(db).send("assistant", "director", "note", "Hen gillar hackathons")
        return RunResult(text='{"reply": "Två saker."}', trace=task.trace)

    result = await ClaudeGate(db, tmp_path, runner=runner).run(ClaudeTask(kind="chat", prompt="x", priority=Priority.CHAT))
    await db.insert("messages", telegram_message_id=2, direction="out", text="Två saker.")   # the reply, sent after the run
    (mem / "profile.md").write_text("Julius, gillar hackathons")
    await MemoryRepo(mem).commit_if_changed("chat", by="assistant")
    w = await office.work(result.run_id)
    assert w["task"] == "Conversation" and w["about"][0]["text"] == "Vad har du hittat?" and w["answer"] == "Två saker."
    assert w["tools"][0]["tool"] == "Looked at the board" and w["posts"][0]["to"] == "Director"
    assert w["edits"][0]["file"] == "profile.md" and "gillar hackathons" in w["edits"][0]["patch"]
    work = (await office.detail("desk-assistant"))["work"]
    assert work[0]["summary"].startswith("Answered you: “Vad har du hittat?”") and "sent 1 post" in work[0]["summary"]
    assert "changed its notes" in work[0]["summary"]
