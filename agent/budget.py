"""The budget (instance/granskning/2026-10-20-omgang-1-grunden.md, "Budgeten"): Audax's share of the subscription,
paced by what is left instead of a switch.

Until 19 Oct a weekly warning held all heavy work until the week reset, and the search stood still for five days
without anyone knowing. Now each skill has a tier (agent/vocab.py). The conversation, what the owner asked for and
the cheap pipeline never wait for the budget. The roles' regular work waits when the week's share is used up.
The heavy and exploratory work waits when today's share is used up, and goes on tomorrow. A full stop only
happens when Claude itself refuses, or near the top of the whole week (claude_gate.WEEK_STOP).

What Audax spends is known exactly (claude_runs.cost_usd, in API-equivalent dollars). How big the week is, is an
estimate (settings.budget.week_usd): Claude reports how full the week is only near the top, and that figure counts
the owner's own use too. So the percentages are estimates, shown with Audax's own dollars beside them.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.db import Database
from agent.vocab import task_name, tier_of

log = logging.getLogger(__name__)
UTC = ZoneInfo("UTC")
NIGHT = {"assistant_night", "director_understand", "director_reflect", "researcher_plan", "manager"}   # the night's work
DAY_SLACK = 1.3   # a day may use a little more than a seventh of the week, the week never more than the cap


class Budget:
    def __init__(self, db: Database, settings):
        self.db = db
        self.settings = settings
        self._told: dict[str, float] = {}   # when a skill's wait was last logged, so a retry every 30 min isn't logged every time
        self.waiting: dict[str, tuple[float, str]] = {}   # what waits for the budget right now, and why: for the office and the chat

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    @property
    def weekly(self) -> float:
        return self.settings.budget.weekly_share * self.settings.budget.week_usd

    @property
    def daily(self) -> float:
        return self.weekly / 7

    async def spent(self, since: datetime) -> float:
        row = await self.db.fetchone("SELECT coalesce(sum(cost_usd), 0) FROM claude_runs WHERE created_at >= ?",
                                     (since.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),))
        return float(row[0] or 0)

    def _midnight(self, now: datetime) -> datetime:
        return now.astimezone(self.tz).replace(hour=0, minute=0, second=0, microsecond=0)

    async def status(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(self.tz)
        today = await self.spent(self._midnight(now))
        week = await self.spent(now - timedelta(days=7))
        return {
            "today_usd": round(today, 2), "week_usd": round(week, 2),
            "daily_usd": round(self.daily, 2), "weekly_usd": round(self.weekly, 2),
            "today_pct": round(100 * today / self.daily) if self.daily else None,          # of today's share
            "week_pct": round(100 * week / self.weekly) if self.weekly else None,          # of the week's share
            "week_of_subscription_pct": round(100 * week / self.settings.budget.week_usd, 1),   # estimate
        }

    async def words(self) -> str:
        """Swedish, for the chat and the raw-data page: what Audax has used, and what waits."""
        s = await self.status()
        text = (f"Audax förbrukning: i dag {s['today_usd']} $ ({s['today_pct']} % av dagens andel), senaste 7 dagarna "
                f"{s['week_usd']} $ ({s['week_pct']} % av veckans andel, uppskattat {s['week_of_subscription_pct']} % av hela "
                f"prenumerationens vecka).")
        for kind, (until, why) in self.waiting.items():
            if until > time.time():
                text += f" {task_name(kind)} väntar till {datetime.fromtimestamp(until, self.tz):%a %H:%M}: {why}."
        return text

    async def capped(self, kind: str, now: datetime) -> tuple[float, str] | None:
        """A guest's budget: everything counts, the conversation too. A rolling week never passes the cap; each day may
        use its share (with a little slack for a lively day), and the day's work leaves a reserve for the night's."""
        cap = self.settings.budget.cap_usd_week
        week = await self.spent(now - timedelta(days=7))
        tomorrow = self._midnight(now) + timedelta(days=1)
        if week >= cap:
            return tomorrow.timestamp(), f"budget: veckans tak ({cap:.2f} $) är nått"
        if tier_of(kind) <= 3:
            return None   # what the guest notices (the conversation, a delivery, the cheap pipeline behind it) only meets the week's cap
        day = min(cap / 7 * DAY_SLACK, cap - week + await self.spent(self._midnight(now)))
        today = await self.spent(self._midnight(now))
        # The roles' work is paced by the day; the heavy work of the day stops where the night's reserve begins (26 Oct:
        # a morning's searches used the day's share and held a finding with a deadline that same day).
        limit = day if kind in NIGHT else day * (1 - self.settings.budget.night_reserve)
        if today >= limit:
            return tomorrow.timestamp(), f"budget: dagens del ({limit:.2f} $) är använd" + ("" if kind in NIGHT else ", resten är natten och samtalet")
        return None

    async def wait(self, kind: str, now: datetime | None = None) -> tuple[float, str] | None:
        """None if the skill may run now; else (until when, why) in words for the log and the office."""
        if self.settings.budget.cap_usd_week:
            return await self.capped(kind, now or datetime.now(self.tz))
        tier = tier_of(kind)
        if tier <= 3:
            return None
        self.waiting.pop(kind, None)
        now = now or datetime.now(self.tz)
        if tier == 4:
            week = await self.spent(now - timedelta(days=7))
            if week < self.weekly:
                return None
            until, why = now + timedelta(hours=6), f"veckans andel är använd ({week:.2f} av {self.weekly:.2f} $)"
        else:
            today = await self.spent(self._midnight(now))
            if today < self.daily:
                return None
            until, why = self._midnight(now) + timedelta(days=1), f"dagens andel är använd ({today:.2f} av {self.daily:.2f} $)"
        self.waiting[kind] = (until.timestamp(), why)
        if time.time() - self._told.get(kind, 0) > 3600:
            self._told[kind] = time.time()
            log.info("budget: %s waits until %s: %s", task_name(kind), f"{until:%a %H:%M}", why)
        return until.timestamp(), why
