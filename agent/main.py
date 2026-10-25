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

from agent.budget import Budget
from agent.builder import Builder
from agent.claude_gate import ClaudeGate, Priority
from agent.controls import Controls
from agent.config import assert_subscription_auth, ensure_instance, ensure_workspace, instance_config, load_settings
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
from agent import opportunities, questions
from agent.due import Calendar, follower, starter
from agent.beliefs import write_profile
from agent.items import Items
from agent import routines
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
    settings = load_settings()
    assert_subscription_auth()
    set_owner(settings.owner)
    if not settings.telegram_token:
        raise SystemExit("TELEGRAM_TOKEN saknas i .env")
    settings.workspace.mkdir(parents=True, exist_ok=True)   # a new home has none yet (25 Oct: a friend's first start)
    (settings.workspace / "CLAUDE.md").write_text(prompt("claude_md"))  # personalized agent instructions
    db = Database(settings.database)
    await db.open()
    from agent import trail

    trail.attach(db)   # the office's event log (agent/trail.py)
    # A run cut off by a restart never finished: say so, rather than leaving it looking like it is still running.
    await db.execute("UPDATE claude_runs SET is_error = 1, error = 'interrupted', finished_at = created_at WHERE finished_at IS NULL")

    budget = Budget(db, settings)
    gate = ClaudeGate(db, settings.workspace, budget=budget)  # the background lane: roles queue here, one run at a time
    # The conversation lane never waits for background work; a guest's conversation counts against its cap too.
    chat_gate = ClaudeGate(db, settings.workspace, quota=gate.quota, budget=budget if settings.budget.cap_usd_week else None)
    memory = MemoryRepo(settings.workspace / "memory")
    if made := ensure_workspace(settings.workspace):   # a new home's memory, from the templates
        log.info("created the workspace: %s", ", ".join(made))

    async def profile_watch(db_, item) -> None:   # the profile is a view of the beliefs (agent/beliefs.py)
        if item["form"] == "belief":
            await write_profile(db_, memory.path, settings.owner or "ägaren", settings.timezone)

    Items.watchers.append(profile_watch)
    # Watched sources and missions are routines (agent/routines.py): moved from the files once, kept in memory, and
    # what runs follows when one changes.
    await routines.import_files(db)
    await routines.import_queries(db)
    await routines.refresh(db)
    Items.watchers.append(routines.watch)
    Items.watchers.append(follower(settings.timezone))   # a promise that waited for something is due when it is done
    Items.watchers.append(questions.route)   # a question to the world becomes a step; a step or experiment answers its question
    Items.watchers.append(opportunities.follow)   # what the owner goes for keeps its opportunity's link and dates
    Items.watchers.append(starter(lambda role, why: wake(role, why)))   # a research step is done now, not on its date (wake is set below)
    if await write_profile(db, memory.path, settings.owner or "ägaren", settings.timezone):
        await memory.commit_if_changed("profilen skriven ur uppfattningarna")
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
    controls.budget = budget
    bot.controls = controls
    controls.speak = bot.chat.speak  # results the owner asked for come in the one voice
    bot.chat.controls = controls
    builder = None
    if settings.builder:   # changing the engine is the host's alone: off in a friend's instance
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

    def wake(role: str, reason: str = "the owner's words") -> None:
        runs = {"director": (director.run, ["understand"]), "manager": (manager.run, []), "researcher": (planner.run, [reason])}
        if role not in runs:
            return  # other roles read their inbox at their next run
        if role in last_wake and datetime.now() - last_wake[role] < timedelta(minutes=30):
            return  # the pending posts are picked up by the next run anyway
        last_wake[role] = datetime.now()
        job, args = runs[role]
        scheduler.add_job(job, "date", run_date=datetime.now() + timedelta(seconds=20), args=args, id=f"{role}_now", replace_existing=True)
        log.info("woke %s: %s", role, reason)

    Posts.waker = wake

    async def process_new_events() -> None:
        if await triage.run():
            await dispatcher.run(force=True)

    ingest = Ingest(db, process_new_events, extract=claude_extractor(gate, settings.models.triage))
    routines.on_streams_changed.append(ingest.restart_streams)   # a watch added, paused or retuned runs as it now is

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
            if problem.startswith("Ingen lyckad Claude-körning") and gate.paused_until(Priority.RESEARCH) is not None:
                continue   # the quota is paused: that is known and shown under /data/, not a fault
            key = "health.alerted." + problem.split("(")[0].strip()[:60]
            last = await db.kv_get(key)
            if last and datetime.fromisoformat(last) > datetime.now() - timedelta(hours=12):
                continue
            await db.kv_set(key, datetime.now().isoformat())
            await bot.send(f"Något fungerar inte som det ska: {problem}.", origin="command")
            log.warning("health: %s", problem)

    scheduler.add_job(health_watch, "interval", minutes=30, id="health", max_instances=1)

    # The calendar (agent/due.py): wakes the owner of a step, an experiment or a question when its date comes.
    async def quiet_now() -> bool:
        return settings.quiet_hours.contains(datetime.now(ZoneInfo(settings.timezone)).time()) or bool(await dispatcher.silenced_until())

    calendar = Calendar(db, settings.timezone, wake=wake, speak=bot.chat.speak, quiet=quiet_now)
    scheduler.add_job(calendar.tick, "interval", minutes=5, next_run_time=datetime.now() + timedelta(minutes=1), id="calendar",
                      max_instances=1, coalesce=True)
    # The conversation's window (agent/chat.py): a big, quiet conversation is folded into its summary.
    scheduler.add_job(bot.chat.tidy, "interval", minutes=10, id="chat_window", max_instances=1, coalesce=True)

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
        if await db.kv_get("night.force_reflect"):   # deploy/audax night reflect
            await db.kv_set("night.force_reflect", None)
            return await director.run("reflect") is not None
        if await director.reflection_due() and await director.run("reflect") is not None:
            return True  # the reflection, as often as Director's own routine says (layer 2)
        return await director.run("understand") is not None

    async def researcher_step() -> bool:
        if await db.kv_get("night.force_researcher"):   # a replay of the night (agent/night.py)
            await db.kv_set("night.force_researcher", None)
            return await planner.run("a replay of last night") is not None
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
    if settings.telegram_chat_id is None:   # nobody to work for yet (a friend's instance before the invite): only /start answers
        log.info("waiting for the owner to open the invite; nothing else runs")
        await asyncio.Event().wait()
    if settings.telegram_chat_id and memory.is_new() and not await db.kv_get("onboarding.started"):
        await db.kv_set("onboarding.started", datetime.now().isoformat())   # a new owner who just joined: get to know them
        await bot.chat.start_interview()
    scheduler.start()
    await ingest.start()
    if builder is not None:
        await builder.on_startup()
    watcher.start()
    web_site = None
    if settings.site.enabled:
        office = Office(db, memory.path, settings.timezone, status=gate.status, budget=budget)
        app = site.build_app(db, office, {"memory": memory.path, "config": INSTANCE / "config"})
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
