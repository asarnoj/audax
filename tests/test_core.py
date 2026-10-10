import asyncio
import time
from datetime import time as dtime

import pytest
from claude_agent_sdk import RateLimitInfo

from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused, RunResult
from agent.config import QuietHours, load_settings
from agent.db import MIGRATIONS, Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


def task(kind: str, priority: Priority) -> ClaudeTask:
    return ClaudeTask(kind=kind, prompt=kind, priority=priority)


# --- db -------------------------------------------------------------------


async def test_migrations_applied_and_idempotent(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    await d.close()
    await d.open()  # re-opening must not re-run migrations
    (version,) = await (await d.conn.execute("PRAGMA user_version")).fetchone()
    assert version == len(MIGRATIONS)
    await d.close()


async def test_kv_roundtrip(db):
    assert await db.kv_get("x", "default") == "default"
    await db.kv_set("x", {"session": "abc"})
    await db.kv_set("x", {"session": "def"})
    assert await db.kv_get("x") == {"session": "def"}


# --- config ---------------------------------------------------------------


def test_quiet_hours_across_midnight():
    q = QuietHours(start=dtime(23, 0), end=dtime(7, 30))
    assert q.contains(dtime(23, 30)) and q.contains(dtime(3, 0))
    assert not q.contains(dtime(7, 30)) and not q.contains(dtime(12, 0))


def test_example_settings_load(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    s = load_settings()
    assert s.telegram_chat_id == 42
    assert s.models.triage


# --- gate -----------------------------------------------------------------


async def test_gate_runs_one_at_a_time_in_priority_order(db, tmp_path):
    order: list[str] = []
    release_first = asyncio.Event()
    running = 0

    async def runner(t, ws, on_rl):
        nonlocal running
        running += 1
        assert running == 1, "more than one concurrent Claude run"
        order.append(t.kind)
        if t.kind == "first":
            await release_first.wait()
        running -= 1
        return RunResult(text="ok")

    gate = ClaudeGate(db, tmp_path, runner=runner)
    first = asyncio.create_task(gate.run(task("first", Priority.BACKGROUND)))
    await asyncio.sleep(0)
    others = [
        asyncio.create_task(gate.run(task("manager", Priority.MANAGER))),
        asyncio.create_task(gate.run(task("research", Priority.RESEARCH))),
        asyncio.create_task(gate.run(task("chat", Priority.CHAT))),
    ]
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.gather(first, *others)

    assert order == ["first", "chat", "research", "manager"]
    rows = await db.fetchall("SELECT kind FROM claude_runs")
    assert len(rows) == 4


async def test_cancelled_waiter_does_not_block_queue(db, tmp_path):
    gate_open = asyncio.Event()

    async def runner(t, ws, on_rl):
        if t.kind == "first":
            await gate_open.wait()
        return RunResult(text=t.kind)

    gate = ClaudeGate(db, tmp_path, runner=runner)
    first = asyncio.create_task(gate.run(task("first", Priority.CHAT)))
    await asyncio.sleep(0)
    doomed = asyncio.create_task(gate.run(task("doomed", Priority.CHAT)))
    later = asyncio.create_task(gate.run(task("later", Priority.BACKGROUND)))
    await asyncio.sleep(0)
    doomed.cancel()
    gate_open.set()
    assert (await later).text == "later"
    await first


async def test_rate_limit_warning_pauses_background_but_not_chat(db, tmp_path):
    async def runner(t, ws, on_rl):
        on_rl(RateLimitInfo(status="allowed_warning", resets_at=int(time.time()) + 600, rate_limit_type="five_hour", utilization=0.9))
        return RunResult(text="ok")

    gate = ClaudeGate(db, tmp_path, runner=runner)
    await gate.run(task("chat", Priority.CHAT))

    with pytest.raises(QuotaPaused):
        await gate.run(task("research", Priority.RESEARCH))
    assert (await gate.run(task("urgent", Priority.URGENT))).text == "ok"


async def test_expired_pause_is_lifted(db, tmp_path):
    async def runner(t, ws, on_rl):
        on_rl(RateLimitInfo(status="rejected", resets_at=int(time.time()) - 1))
        return RunResult(text="ok")

    gate = ClaudeGate(db, tmp_path, runner=runner)
    await gate.run(task("chat", Priority.CHAT))
    assert gate.paused_until(Priority.BACKGROUND) is None


async def test_runner_exception_becomes_error_result(db, tmp_path):
    async def runner(t, ws, on_rl):
        raise RuntimeError("cli crashed")

    gate = ClaudeGate(db, tmp_path, runner=runner)
    r = await gate.run(task("x", Priority.CHAT))
    assert r.is_error and "cli crashed" in r.error
    row = await db.fetchone("SELECT is_error FROM claude_runs")
    assert row["is_error"] == 1


async def test_backup_keeps_newest(db, tmp_path):
    backups = tmp_path / "b"
    backups.mkdir()
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (backups / f"agent-{d}.db").write_text("x")
    target = await db.backup(backups, keep=2)
    assert target.exists() and target.stat().st_size > 0
    assert len(list(backups.glob("agent-*.db"))) == 2
