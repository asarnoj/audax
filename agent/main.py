"""Entry point: `uv run python -m agent.main`."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agent.builder import Builder
from agent.claude_gate import ClaudeGate
from agent.controls import Controls
from agent.config import assert_subscription_auth, ensure_instance, instance_config, load_settings
from agent.director import Director
from agent.inspect import Inspector
from agent.prompts import prompt, set_owner
from agent.web import WebReader
from agent.mail import MailReader
from agent.windows import GitHubReader, all_windows
from agent.db import Database
from agent.manager import Manager
from agent.memory import MemoryRepo
from agent.pipeline.dispatcher import Dispatcher
from agent.pipeline.ingest import Ingest
from agent.sources.pages import claude_extractor
from agent.pipeline.outbox import Outbox, add_candidate
from agent.pipeline.triage import Triage
from agent.reload import ConfigWatcher
from agent.research import Researcher, load_research
from agent.schedule import Schedule, describe_change, utc_iso
from agent.sources.calendar import build_sources, load_calendar_configs
from agent.telegram_bot import TelegramBot

log = logging.getLogger("agent")


async def run() -> None:
    if created := ensure_instance():
        log.info("created instance config from examples: %s", ", ".join(created))
    settings = load_settings()
    assert_subscription_auth()
    set_owner(settings.owner)
    (settings.workspace / "CLAUDE.md").write_text(prompt("claude_md"))  # personalized agent instructions
    if not settings.telegram_token:
        raise SystemExit("TELEGRAM_TOKEN saknas i .env")

    settings.workspace.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database)
    await db.open()

    gate = ClaudeGate(db, settings.workspace)
    memory = MemoryRepo(settings.workspace / "memory")
    if not (memory.path / ".git").exists():
        raise SystemExit("workspace/memory saknas: kör ./deploy/init-memory.sh först")
    cal_configs, cal_options = load_calendar_configs()
    schedule = Schedule(settings, db, build_sources(cal_configs, settings.timezone), cal_options.get("window_days", 21))
    bot = TelegramBot(settings, db, gate, memory, schedule)
    outbox = Outbox(db, schedule, bot.send)
    await outbox.recover()  # deliveries interrupted by a crash or restart
    manager = Manager(settings, db, gate, memory)
    outbox.speak = bot.chat.speak  # one voice: the chat session writes proactive messages too
    dispatcher = Dispatcher(settings, db, gate, memory, schedule, outbox, bot.send_poll, manager, bot.send_proposal, bot.chat.inbox)
    bot.manager = manager
    triage = Triage(settings, db, gate, memory)
    researcher = Researcher(settings, db, gate, memory)

    async def sync_calendars() -> None:
        changes = await schedule.sync()
        for change in changes:
            soon = (change.start - schedule.now()).total_seconds() < 24 * 3600
            await add_candidate(
                db,
                kind="schedule_change",
                title=change.summary,
                body=describe_change(change),
                features={"time_sensitivity": "high" if soon else "medium", "relevance": "high", "actionability": "medium"},
                expires_at=utc_iso(change.start),
            )
        await dispatcher.run(force=bool(changes))

    async def research_tick(task_id: str | None = None) -> int:
        """One research task per tick, so research never hogs Claude for long stretches.

        With task_id (from /sok) that task runs now regardless of schedule. Returns new findings."""
        if task_id is None and schedule.settings.quiet_hours.contains(schedule.now().time()):
            return 0  # research at night would only queue up for the morning; run it during the day
        deep = False
        if task_id is not None:
            tasks = [t for t in load_research() if t.id == task_id]
        else:
            tasks = (await researcher.due())[:1]
            if not tasks and (d := await researcher.due_deep()):  # nothing routine due: time for a weekly deep sweep
                tasks, deep = [d], True
        found = 0
        for t in tasks:
            found += await researcher.run(t, deep=deep)
        if await triage.run():
            await dispatcher.run(force=True)
        return found

    controls = Controls(db, dispatcher, research_tick, notify=bot.send)
    bot.controls = controls
    bot.chat.controls = controls
    builder = Builder(settings, db, ClaudeGate(db, settings.workspace, quota=gate.quota), on_ready=bot.send_change, notify=bot.send)
    bot.builder = builder
    bot.chat.builder = builder

    director = Director(settings, db, gate, memory, schedule)
    mail = MailReader(tz=settings.timezone) if MailReader.configured() else None  # read-only; Manager never gets it
    director.windows = lambda: all_windows(mail)
    director.mail = bot.chat.mail = mail
    if os.getenv("GITHUB_USER") or os.getenv("GITHUB_TOKEN"):
        director.github = bot.chat.github = GitHubReader()
    web = WebReader(db, github=director.github)
    director.web = bot.chat.web = researcher.web = web

    # Worker -> Director/Manager: a note marked act_now triggers that mind's pass soon instead of at night.
    # Coalesced (one queued run per mind) and spaced (at most every 30 min), so a chatty day can't loop them.
    last_wake: dict[str, datetime] = {}

    def wake(owner: str) -> None:
        if owner in last_wake and datetime.now() - last_wake[owner] < timedelta(minutes=30):
            return  # the pending notes are picked up by the next run anyway
        last_wake[owner] = datetime.now()
        job, args = (director.run, ["understand"]) if owner == "director" else (manager.run, [])
        scheduler.add_job(job, "date", run_date=datetime.now() + timedelta(seconds=20), args=args, id=f"{owner}_now", replace_existing=True)
        log.info("woke %s on request from the chat", owner)

    bot.chat.wake_director = wake

    async def process_new_events() -> None:
        if await triage.run():
            await dispatcher.run(force=True)

    ingest = Ingest(db, process_new_events, extract=claude_extractor(gate, settings.models.triage))

    async def reload_settings() -> None:
        fresh = load_settings()
        for name in type(settings).model_fields:  # update in place: every component holds this object
            setattr(settings, name, getattr(fresh, name))

    async def reload_sources() -> None:
        configs, _ = load_calendar_configs()
        schedule.set_sources(build_sources(configs, settings.timezone))
        await ingest.restart_streams()

    watcher = ConfigWatcher()
    watcher.watch(instance_config("settings.yaml"), reload_settings)
    watcher.watch(instance_config("sources.yaml"), reload_sources)

    # Run missed jobs late instead of skipping them (laptop sleep, slow Pi); coalesce repeats into one.
    scheduler = AsyncIOScheduler(timezone=settings.timezone, job_defaults={"misfire_grace_time": None, "coalesce": True})
    scheduler.add_job(sync_calendars, "interval", minutes=cal_options.get("poll_minutes", 30), next_run_time=datetime.now(),
                      id="calendar_sync", max_instances=1, coalesce=True)
    scheduler.add_job(dispatcher.run, "interval", minutes=10, id="dispatcher", max_instances=1, coalesce=True)
    inspector = Inspector(db, settings.timezone)

    async def health_watch() -> None:
        """Silence must never mean broken: real failures are reported, once per problem and half day."""
        for problem in await inspector.health():
            key = "health.alerted." + problem.split("(")[0].strip()[:60]
            last = await db.kv_get(key)
            if last and datetime.fromisoformat(last) > datetime.now() - timedelta(hours=12):
                continue
            await db.kv_set(key, datetime.now().isoformat())
            await bot.send(f"Något fungerar inte som det ska: {problem}.", origin="command")
            log.warning("health: %s", problem)

    scheduler.add_job(health_watch, "interval", minutes=30, id="health", max_instances=1)

    async def snapshot() -> None:
        """Heartbeat + the scheduler's own view of its jobs, for `audax jobs` (inspection runs in another process)."""
        jobs = [
            {"id": j.id, "next": j.next_run_time.isoformat() if j.next_run_time else None, "trigger": str(j.trigger)}
            for j in scheduler.get_jobs()
        ]
        await db.kv_set("engine.snapshot", {"at": datetime.now().astimezone().isoformat(), "pid": os.getpid(), "jobs": jobs})

    scheduler.add_job(snapshot, "interval", minutes=5, next_run_time=datetime.now() + timedelta(seconds=30), id="snapshot")
    scheduler.add_job(manager.run, "cron", hour=3, minute=30, id="manager", max_instances=1)
    scheduler.add_job(director.run, "cron", hour=4, minute=15, args=["understand"], id="director_understand", max_instances=1)
    scheduler.add_job(director.run, "cron", day_of_week="mon,wed,sat", hour=5, minute=0, args=["reflect"], id="director_reflect", max_instances=1)
    scheduler.add_job(db.backup, "cron", hour=3, minute=15, args=[settings.database.parent / "backups"], id="backup")
    scheduler.add_job(research_tick, "interval", minutes=30, next_run_time=datetime.now() + timedelta(minutes=2),
                      id="research", max_instances=1, coalesce=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await bot.start()
    scheduler.start()
    await ingest.start()
    await builder.on_startup()
    watcher.start()
    log.info("agent running")
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        await watcher.stop()
        await ingest.stop()
        scheduler.shutdown(wait=False)
        await bot.stop()
        await db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
