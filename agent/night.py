"""The night sweep (docs/MODEL.md, "Natten"): the slow tasks run once, in order, while the owner sleeps.

1. Assistant: tidies the memory, writes its working memory and the conversation summary, books its promises.
2. Director: the daily review, or the reflection on reflection nights (falling back to the review if the
   reflection can't run, e.g. the weekly quota holds heavy work).
3. Researcher: plans, if an order, feedback, a thin portfolio or a quiet mission calls for it.
4. Manager: reviews the day and plans the schedule with the fresh picture.

The start time comes from data: when the owner usually writes their last message of the evening, plus a margin,
within quiet hours. A step that fails is tried again at the next check (every ten minutes) and is skipped after three
attempts, so one failing step can't keep Manager from running. A quota pause isn't a failure: the sweep waits for it
to lift (within quiet hours) without spending attempts. Progress is kept per night, so a restart continues where the
sweep left off.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.config import Settings
from agent.db import Database

log = logging.getLogger(__name__)

Step = Callable[[], Awaitable[bool]]  # True when done (or nothing to do), False to try again
STATE_KEY = "night.state"
# What each role's "since my last run" is measured from: a replay sets them back to the evening before last night's
# sweep, so the sweep reads the whole day again, as a real night would (24 Oct).
SINCE_KEYS = ("assistant.last_night", "director.last_understand", "director.last_reflect", "manager.last_run", "researcher.last_plan")
FORCE_KEY = "night.force"   # set by the maintainer (deploy/audax night): one whole sweep now, whatever the hour
MARGIN = timedelta(minutes=60)  # after the owner's usual last message
LATEST_BEFORE_END = timedelta(minutes=150)  # the sweep must start this long before quiet hours end
AWAKE = timedelta(minutes=45)  # the owner wrote this recently: probably still awake
MAX_ATTEMPTS = 3


class Night:
    def __init__(self, settings: Settings, db: Database, steps: list[tuple[str, Step]], paused: Callable[[], bool] = lambda: False):
        self.settings = settings
        self.db = db
        self.steps = steps
        self.paused = paused  # the background quota is paused: wait, don't count it as an attempt
        self.running = False

    async def rewind(self, now: datetime) -> None:
        """Last night again: every role reads from the evening before last night's sweep, and the steps that only run
        when something is due run anyway."""
        last_night = datetime.fromisoformat(self.night_of(now - timedelta(days=1))).replace(tzinfo=now.tzinfo, hour=18)
        for key in SINCE_KEYS:
            await self.db.kv_set(key, last_night.isoformat())
        await self.db.kv_set("night.force_researcher", True)
        log.info("night replay: the roles read from %s", f"{last_night:%Y-%m-%d %H:%M}")

    def night_of(self, now: datetime) -> str:
        """The evening a night belongs to: 02:00 on the 14th is the night of the 13th."""
        return (now - timedelta(hours=12)).date().isoformat()

    def _window(self, now: datetime) -> tuple[datetime, datetime]:
        q = self.settings.quiet_hours
        evening = datetime.fromisoformat(self.night_of(now)).replace(tzinfo=now.tzinfo)
        start = evening.replace(hour=q.start.hour, minute=q.start.minute)
        end = (evening + timedelta(days=1 if q.end <= q.start else 0)).replace(hour=q.end.hour, minute=q.end.minute)
        return start, end

    async def usual_bedtime(self, now: datetime) -> datetime:
        """When the owner usually writes their last message of the evening (the 80th percentile, last two weeks)."""
        start, end = self._window(now)
        since = (now - timedelta(days=15)).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")   # from `now`, not the clock
        rows = await self.db.fetchall("SELECT created_at FROM messages WHERE direction = 'in' AND created_at >= ?", (since,))
        lasts: dict[str, timedelta] = {}
        for r in rows:
            t = datetime.fromisoformat(r["created_at"] + "+00:00").astimezone(now.tzinfo)
            night = self.night_of(t)
            offset = t - datetime.fromisoformat(night).replace(tzinfo=now.tzinfo, hour=18)  # minutes after 18:00 that evening
            if timedelta(0) <= offset <= timedelta(hours=12):
                lasts[night] = max(lasts.get(night, offset), offset)
        if not lasts:
            return start
        values = sorted(lasts.values())
        usual = values[min(len(values) - 1, int(0.8 * len(values)))]
        return datetime.fromisoformat(self.night_of(now)).replace(tzinfo=now.tzinfo, hour=18) + usual

    async def planned_start(self, now: datetime) -> datetime:
        start, end = self._window(now)
        return min(max(start + timedelta(minutes=30), await self.usual_bedtime(now) + MARGIN), end - LATEST_BEFORE_END)

    async def _state(self, night: str) -> dict:
        state = await self.db.kv_get(STATE_KEY) or {}
        return state if state.get("night") == night else {"night": night, "done": [], "attempts": {}}

    async def tick(self, now: datetime) -> None:
        """Called every ten minutes: starts or continues tonight's sweep when it is time."""
        if self.running:
            return
        if force := await self.db.kv_get(FORCE_KEY):   # a sweep asked for by hand runs now, in full; tonight's runs as usual
            await self.db.kv_set(FORCE_KEY, None)
            if force == "replay":
                await self.rewind(now)
            only = force if force in dict(self.steps) else None   # one role's night step, e.g. one that failed
            if only == "researcher":
                await self.db.kv_set("night.force_researcher", True)
            log.info("night sweep started by hand%s", " (a replay of last night)" if force == "replay" else f" ({only} only)" if only else "")
            self.running = True
            try:
                done = [n for n, _ in self.steps if only and n != only]
                state = {"night": f"by hand {now:%Y-%m-%d %H:%M}", "done": done, "attempts": {}}
                await self.db.kv_set(STATE_KEY, state)   # kept, so a sweep that has to wait goes on later
                await self._sweep(state)
            finally:
                self.running = False
            return
        held = await self.db.kv_get(STATE_KEY) or {}
        if str(held.get("night", "")).startswith("by hand") and len(held.get("done", [])) < len(self.steps):
            self.running = True   # a sweep started by hand that waited (for the quota, say) goes on where it stopped
            try:
                await self._sweep(held)
            finally:
                self.running = False
            return
        start, end = self._window(now)
        if not (start <= now < end):
            return
        state = await self._state(self.night_of(now))
        if len(state["done"]) == len(self.steps):
            return
        planned = await self.planned_start(now)
        if now < planned:
            return
        last_in = await self.db.fetchone("SELECT max(created_at) AS t FROM messages WHERE direction = 'in'")
        if last_in and last_in["t"] and now < end - LATEST_BEFORE_END:
            if now - datetime.fromisoformat(last_in["t"] + "+00:00").astimezone(now.tzinfo) < AWAKE:
                return  # still awake: wait a little
        self.running = True
        try:
            await self._sweep(state)
        finally:
            self.running = False

    async def _sweep(self, state: dict) -> None:
        for name, step in self.steps:
            if name in state["done"]:
                continue
            if self.paused():
                log.info("night sweep waits at %s: the quota is paused", name)
                return
            try:
                ok = await step()
            except Exception:
                log.exception("night step %s failed", name)
                ok = False
            if not ok and self.paused():
                log.info("night step %s hit a quota pause; it runs when the pause lifts", name)
                return
            tries = state["attempts"].get(name, 0) + 1
            state["attempts"][name] = tries
            if ok or tries >= MAX_ATTEMPTS:
                state["done"].append(name)
                if not ok:
                    log.warning("night step %s skipped after %d attempts", name, tries)
                await self.db.kv_set(STATE_KEY, state)
                continue
            await self.db.kv_set(STATE_KEY, state)
            log.info("night step %s didn't finish; the sweep continues from here at the next check", name)
            return
        log.info("night sweep done: %s", ", ".join(state["done"]))

