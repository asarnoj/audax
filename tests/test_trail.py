import json
import subprocess

import pytest

from agent import trail
from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, RunResult
from agent.db import Database
from agent.memory import MemoryRepo
from agent.posts import Posts


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    trail.attach(d)
    yield d
    trail.attach(None)
    await d.close()


async def test_what_a_run_writes_carries_its_id_and_the_office_log_records_it(db, tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    subprocess.run(["git", "-C", str(mem), "init", "-q"], check=True)
    posts = Posts(db)

    async def runner(task, ws, on_rl):
        await posts.send("director", "researcher", "order", "Leta medgrundare")   # written during the run
        return RunResult(text="ok")

    result = await ClaudeGate(db, tmp_path, runner=runner).run(ClaudeTask(kind="director_understand", prompt="x", priority=Priority.MANAGER))
    (mem / "goals.md").write_text("Mål")
    await MemoryRepo(mem).commit_if_changed("director understand", by="director")   # applied after the run, same task

    post = await db.fetchone("SELECT run_id FROM posts")
    assert post["run_id"] == result.run_id
    run = await db.fetchone("SELECT kind, finished_at FROM claude_runs WHERE id = ?", (result.run_id,))
    assert run["kind"] == "director_understand" and run["finished_at"]
    log = subprocess.run(["git", "-C", str(mem), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout
    assert f"[#R{result.run_id}]" in log
    events = [(r["kind"], r["role"], r["run_id"], json.loads(r["data"])) for r in await db.fetchall("SELECT * FROM office_events ORDER BY id")]
    assert [e[0] for e in events] == ["start", "post", "end", "save"]
    assert all(e[2] == result.run_id for e in events)
    assert events[1][3] == {"to": "researcher", "what": "order", "ref": "#P1"} and events[3][1] == "director"
