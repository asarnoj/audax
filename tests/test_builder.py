import asyncio
import subprocess

import pytest

from agent.builder import Builder
from agent.claude_gate import ClaudeGate, RunResult
from agent.config import load_settings
from agent.db import Database


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
async def env(tmp_path):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "agent").mkdir()
    (repo / "config" / "sources.yaml").write_text("streams: []\n")
    (repo / "agent" / "x.py").write_text("X = 1\n")
    git("init", "-q", "-b", "main", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    db = Database(tmp_path / "t.db")
    await db.open()
    yield repo, db, tmp_path
    await db.close()


def make(db, repo, tmp_path, edit=None, tests_ok=True, structured=None):
    async def runner(task, ws, on_rl):
        if edit:
            edit(task.cwd)
        return RunResult(text="", structured=structured or {"summary": "Lade till en källa.", "risk": "low", "needs_followup": None})

    ready, notes = [], []

    async def on_ready(cid):
        ready.append(cid)

    async def notify(text):
        notes.append(text)

    async def verify(wt):
        return tests_ok, "1 passed" if tests_ok else "1 failed"

    b = Builder(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), on_ready, notify, repo=repo, verify=verify)
    b.builds = tmp_path / "builds"  # keep test worktrees out of /var/tmp
    return b, ready, notes


async def wait(ready):
    for _ in range(200):
        if ready:
            return
        await asyncio.sleep(0.02)


def add_source(wt):
    (wt / "config" / "sources.yaml").write_text("streams:\n  - id: uni\n")


async def test_build_then_deploy_config_change_goes_live_without_restart(env):
    repo, db, tmp = env
    b, ready, _ = make(db, repo, tmp, edit=add_source)
    cid = await b.request("Lägg till Uni", "Uni-events som källa")
    with pytest.raises(RuntimeError):
        await b.request("En till", "samtidigt")  # one build at a time
    await wait(ready)

    row = await db.fetchone("SELECT * FROM changes WHERE id = ?", (cid,))
    assert row["status"] == "ready" and "sources.yaml" in row["diffstat"]
    assert "id: uni" in await b.diff(cid)
    assert "uni" not in (repo / "config" / "sources.yaml").read_text()  # nothing live before approval

    assert "gäller direkt" in await b.deploy(cid)
    assert "id: uni" in (repo / "config" / "sources.yaml").read_text()
    assert not b._worktree(cid).exists()
    assert "change/" not in git("branch", cwd=repo)
    assert "Driftsatt" not in await b.deploy(cid)  # can only be deployed once


async def test_code_change_needs_restart_and_announces_after(env):
    repo, db, tmp = env
    b, ready, notes = make(db, repo, tmp, edit=lambda wt: (wt / "agent" / "x.py").write_text("X = 2\n"))
    cid = await b.request("Ändra X", "x")
    await wait(ready)
    assert "Starta om" in await b.deploy(cid)  # not under systemd in tests
    await b.on_startup()
    assert notes == ['Tillbaka. Ändringen "Ändra X" är live.']


async def test_failing_tests_block_deploy(env):
    repo, db, tmp = env
    b, ready, _ = make(db, repo, tmp, edit=add_source, tests_ok=False)
    cid = await b.request("Trasig", "x")
    await wait(ready)
    row = await db.fetchone("SELECT status, test_output FROM changes WHERE id = ?", (cid,))
    assert row["status"] == "failed" and "1 failed" in row["test_output"]
    assert "går inte" in await b.deploy(cid)
    assert "Slängd" in await b.discard(cid)
    assert not b._worktree(cid).exists()


async def test_no_change_is_reported(env):
    repo, db, tmp = env
    b, ready, notes = make(db, repo, tmp, structured={"summary": "Det går inte säkert.", "risk": "high"})
    cid = await b.request("Omöjligt", "x")
    await wait(ready)
    assert (await db.fetchone("SELECT status FROM changes WHERE id = ?", (cid,)))["status"] == "failed"
    assert "ingen ändring" in notes[0]


async def test_failing_tests_get_fix_rounds_with_output(env):
    repo, db, tmp = env
    prompts, results = [], iter([False, True])

    async def runner(task, ws, on_rl):
        prompts.append((task.prompt, task.resume))
        add_source(task.cwd)
        return RunResult(text="", session_id="s1", structured={"summary": "ok", "risk": "low"})

    async def verify(wt):
        ok = next(results)
        return ok, "passed" if ok else "FAILED test_x - AssertionError"

    ready = []

    async def on_ready(cid):
        ready.append(cid)

    async def notify(text):
        pass

    b = Builder(load_settings(), db, ClaudeGate(db, tmp, runner=runner), on_ready, notify, repo=repo, verify=verify)
    b.builds = tmp / "builds"
    cid = await b.request("Fixa", "x")
    await wait(ready)
    assert (await db.fetchone("SELECT status FROM changes WHERE id = ?", (cid,)))["status"] == "ready"
    assert len(prompts) == 2 and "FAILED test_x" in prompts[1][0] and prompts[1][1] == "s1"
