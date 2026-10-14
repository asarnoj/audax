"""Entry point: `uv run python -m agent.main`."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agent.builder import Builder
from agent.claude_gate import ClaudeGate, Priority
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
from agent.posts import Posts
from agent.pipeline.dispatcher import Dispatcher
from agent.pipeline.ingest import Ingest
from agent.sources.pages import claude_extractor
from agent.pipeline.outbox import Outbox, add_candidate
from agent.pipeline.triage import Triage
from agent.reload import ConfigWatcher
from agent.research import Researcher, load_research
from agent.research_plan import ResearchPlanner
from agent.assistant_night import AssistantNight
from agent.night import Night
from agent import site
from agent.config import INSTANCE
from agent.office import Office
from agent.schedule import Schedule, describe_change, utc_iso
from agent.sources.calendar import build_sources, load_calendar_configs
from agent.telegram_bot import TelegramBot

log = logging.getLogger("agent")


async def move_open_questions(memory: MemoryRepo, posts: Posts) -> None:
    """open-questions.md is replaced by the internal mail (docs/MODEL.md): its content goes to Director, once."""
    path = memory.path / "open-questions.md"
    if not path.exists():
        return
    text = re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.DOTALL)
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if lines:
        await posts.send("assistant", "director", "note", "Luckor i förståelsen som tidigare låg i open-questions.md:\n" + "\n".join(lines))
    path.unlink()
    await memory.commit_if_changed("open-questions.md flyttad till postgången")
    log.info("moved open-questions.md to Director's inbox (%d lines)", len(lines))


async def move_search_maps(memory: MemoryRepo) -> None:
    """The search maps are Researcher's notebook now (docs/MODEL.md): direction/search/ moves to research/maps/, once."""
    old = memory.path / "direction" / "search"
    if not old.exists():
        return
    new = memory.path / "research" / "maps"
    new.mkdir(parents=True, exist_ok=True)
    for f in old.glob("*.md"):
        f.rename(new / f.name)
    old.rmdir()
    await memory.commit_if_changed("sökkartorna flyttade till Researcher (research/maps)")
    log.info("moved the search maps to research/maps")


async def run() -> None:
    if created := ensure_instance():
        log.info("created instance config from examples: %s", ", ".join(created))
    from agent import direction_ops

    try:
        if added := direction_ops.watch_unwatched_companies():
            log.info("watching careers pages for companies without a known ATS: %s", ", ".join(added))
    except Exception:
        log.exception("couldn't check the watchlist for unwatched companies")
    settings = load_settings()
    assert_subscription_auth()
    set_owner(settings.owner)
    (settings.workspace / "CLAUDE.md").write_text(prompt("claude_md"))  # personalized agent instructions
    if not settings.telegram_token:
        raise SystemExit("TELEGRAM_TOKEN saknas i .env")

    settings.workspace.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database)
    await db.open()

    gate = ClaudeGate(db, settings.workspace)  # the background lane: roles queue here, one run at a time
    chat_gate = ClaudeGate(db, settings.workspace, quota=gate.quota)  # the conversation lane: never waits for background work
    memory = MemoryRepo(settings.workspace / "memory")
    if not (memory.path / ".git").exists():
        raise SystemExit("workspace/memory saknas: kör ./deploy/init-memory.sh först")
    cal_configs, cal_options = load_calendar_configs()
    schedule = Schedule(settings, db, build_sources(cal_configs, settings.timezone), cal_options.get("window_days", 21))
    bot = TelegramBot(settings, db, chat_gate, memory, schedule)
    await move_open_questions(memory, Posts(db))
    await move_search_maps(memory)
    outbox = Outbox(db, schedule, bot.send)
    await outbox.recover()  # deliveries interrupted by a crash or restart
    manager = Manager(settings, db, gate, memory)
    outbox.speak = bot.chat.speak  # one voice: the chat session writes proactive messages too
    dispatcher = Dispatcher(settings, db, gate, memory, schedule, outbox, bot.send_poll, manager, bot.send_proposal, bot.chat.inbox)
    manager.agenda = dispatcher.agenda  # Manager owns the schedule; its desk (the dispatcher) fills it during the day
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
            if not tasks and (d := await researcher.due_deep()):  # nothing routine due: a wide sweep Researcher asked for
                tasks, deep = [d], True
        found = 0
        for t in tasks:
            found += await researcher.run(t, deep=deep)
        if await triage.run():
            await dispatcher.run(force=True)
        return found

    controls = Controls(db, dispatcher, research_tick, notify=bot.send)
    bot.controls = controls
    controls.speak = bot.chat.speak  # results the owner asked for come in the one voice
    bot.chat.controls = controls
    builder = Builder(settings, db, ClaudeGate(db, settings.workspace, quota=gate.quota), on_ready=bot.send_change, notify=bot.send)
    bot.builder = builder
    bot.chat.builder = builder

    director = Director(settings, db, gate, memory, schedule)
    mail = MailReader(tz=settings.timezone) if MailReader.configured() else None  # read-only; Manager never gets it
    director.windows = all_windows  # GitHub; the Director adds mail itself, in the daily review only
    director.mail = bot.chat.mail = mail
    if os.getenv("GITHUB_USER") or os.getenv("GITHUB_TOKEN"):
        director.github = bot.chat.github = GitHubReader()
    web = WebReader(db, github=director.github)
    director.web = bot.chat.web = researcher.web = web
    planner = ResearchPlanner(settings, db, gate, memory)  # Researcher's slow task
    planner.web = web

    def start_mission(mission_id: str) -> None:
        scheduler.add_job(research_tick, "date", run_date=datetime.now() + timedelta(seconds=10), args=[mission_id],
                          id=f"mission_{mission_id}", replace_existing=True)

    planner.start_mission = start_mission

    # The internal mail wakes a role when a post from a chain that began with the owner asks for it (agent/posts.py).
    # Coalesced (one queued run per role) and spaced (at most every 30 min), so a chatty day can't flood them.
    last_wake: dict[str, datetime] = {}

    def wake(role: str) -> None:
        runs = {"director": (director.run, ["understand"]), "manager": (manager.run, []), "researcher": (planner.run, ["the owner's words"])}
        if role not in runs:
            return  # other roles read their inbox at their next run
        if role in last_wake and datetime.now() - last_wake[role] < timedelta(minutes=30):
            return  # the pending posts are picked up by the next run anyway
        last_wake[role] = datetime.now()
        job, args = runs[role]
        scheduler.add_job(job, "date", run_date=datetime.now() + timedelta(seconds=20), args=args, id=f"{role}_now", replace_existing=True)
        log.info("woke %s on the owner's behalf", role)

    Posts.waker = wake

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
    scheduler.add_job(dispatcher.tick, "interval", minutes=1, id="schedule", max_instances=1, coalesce=True)  # Python delivers what is due
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
    # The night sweep (agent/night.py): the slow tasks in order, starting when the owner usually sleeps.
    night_pass = AssistantNight(settings, db, gate, memory, dispatcher.agenda)

    async def assistant_step() -> bool:
        return await night_pass.run() is not None

    async def director_step() -> bool:
        tz = ZoneInfo(settings.timezone)
        if (datetime.now(tz) + timedelta(hours=6)).weekday() in (0, 2, 5) and await director.run("reflect") is not None:
            return True  # Monday, Wednesday and Saturday mornings: the reflection
        return await director.run("understand") is not None

    async def researcher_step() -> bool:
        reason = await planner.due()
        return reason is None or await planner.run(reason) is not None

    async def manager_step() -> bool:
        return await manager.run() is not None

    night = Night(
        settings, db,
        [("assistant", assistant_step), ("director", director_step), ("researcher", researcher_step), ("manager", manager_step)],
        paused=lambda: gate.paused_until(Priority.MANAGER) is not None,
    )

    async def night_tick() -> None:
        await night.tick(datetime.now(ZoneInfo(settings.timezone)))

    scheduler.add_job(night_tick, "interval", minutes=10, id="night", max_instances=1, coalesce=True)
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
    web_site = None
    if settings.site.enabled:
        office = Office(db, memory.path, settings.timezone, status=gate.status)
        app = site.build_app(db, office, {"minne": memory.path, "installningar": INSTANCE / "config"})
        web_site = site.start(app, settings.site.host, settings.site.port)
        log.info("office page on http://%s:%d", settings.site.host, settings.site.port)
    log.info("agent running")
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        if web_site:
            web_site[1].should_exit = True
            await asyncio.gather(web_site[0], return_exceptions=True)
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
