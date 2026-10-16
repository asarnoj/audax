import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent.assistant_night import CHAT_SEED_KEY, AssistantNight
from agent.claude_gate import ClaudeGate, RunResult
from agent.config import load_settings
from agent.db import Database
from agent.memory import MemoryRepo
from agent.night import Night

TZ = ZoneInfo("Europe/Stockholm")


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


def night_pass(db, tmp_path, structured=None):
    mem = tmp_path / "memory"
    mem.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=mem, check=True)

    async def runner(task, ws, on_rl):
        return RunResult(text="", structured=structured)

    return AssistantNight(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), MemoryRepo(mem), None), mem


OUT = {"working_memory_md": "Fokus: tentan.", "conversation_summary": "Pratade om tentan och Google.", "promises": [], "summary": ""}


async def test_night_pass_writes_working_memory_and_restarts_a_big_session(db, tmp_path):
    a, mem = night_pass(db, tmp_path, OUT)
    await db.kv_set("chat.session_id", "big")
    await db.execute("INSERT INTO claude_runs (kind, is_error, context_tokens) VALUES ('chat', 0, 109000)")
    assert await a.run() is not None
    assert (mem / "arbetsminne" / "assistant.md").read_text() == "Fokus: tentan.\n"
    assert await db.kv_get(CHAT_SEED_KEY) == "Pratade om tentan och Google."
    assert await db.kv_get("chat.session_id") is None  # the next message starts over from the summary


async def test_a_small_session_is_kept(db, tmp_path):
    a, _ = night_pass(db, tmp_path, OUT)
    await db.kv_set("chat.session_id", "small")
    await db.execute("INSERT INTO claude_runs (kind, is_error, context_tokens) VALUES ('chat', 0, 30000)")
    await a.run()
    assert await db.kv_get("chat.session_id") == "small"


async def test_the_sweep_runs_in_order_after_the_owner_usually_sleeps_and_resumes(db):
    ran = []
    fail_once = {"director": True}

    def step(name):
        async def run():
            ran.append(name)
            if fail_once.pop(name, False):
                return False  # e.g. quota paused: try again at the next check
            return True
        return run

    night = Night(load_settings(), db, [(n, step(n)) for n in ("assistant", "director", "researcher", "manager")])
    for d in range(1, 6):  # the owner usually writes until about 00:30
        t = datetime(2026, 10, 13 - d, 0, 30, tzinfo=TZ) + timedelta(days=1)
        await db.execute("INSERT INTO messages (telegram_message_id, direction, text, created_at) VALUES (1, 'in', 'x', ?)",
                         (t.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S"),))
    planned = await night.planned_start(datetime(2026, 10, 13, 23, 30, tzinfo=TZ))
    assert planned == datetime(2026, 10, 14, 1, 30, tzinfo=TZ)  # usual last message + an hour

    await night.tick(datetime(2026, 10, 14, 0, 0, tzinfo=TZ))
    assert ran == []  # too early
    await night.tick(datetime(2026, 10, 14, 1, 40, tzinfo=TZ))
    assert ran == ["assistant", "director"]  # director didn't finish: stop here
    await night.tick(datetime(2026, 10, 14, 1, 50, tzinfo=TZ))
    assert ran == ["assistant", "director", "director", "researcher", "manager"]  # continues where it left off
    await night.tick(datetime(2026, 10, 14, 2, 0, tzinfo=TZ))
    assert len(ran) == 5  # once a night


async def test_a_quota_pause_makes_the_sweep_wait_instead_of_spending_attempts(db):
    ran = []
    quota = {"paused": True}

    def step(name):
        async def run():
            ran.append(name)
            return not quota["paused"]
        return run

    night = Night(load_settings(), db, [(n, step(n)) for n in ("assistant", "director", "researcher", "manager")],
                  paused=lambda: quota["paused"])
    for minute in range(0, 120, 10):  # two hours of checks while the quota is paused
        await night.tick(datetime(2026, 10, 14, 2, 0, tzinfo=TZ) + timedelta(minutes=minute))
    assert ran == []  # nothing tried, nothing skipped
    assert (await db.kv_get("night.state") or {}).get("done", []) == []

    quota["paused"] = False  # the pause lifts before morning
    await night.tick(datetime(2026, 10, 14, 4, 0, tzinfo=TZ))
    assert ran == ["assistant", "director", "researcher", "manager"]
    state = await db.kv_get("night.state")
    assert state["attempts"] == {"assistant": 1, "director": 1, "researcher": 1, "manager": 1}
