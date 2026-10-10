"""Live reload: config files are watched and applied without restarting the agent.

Prompts (agent/prompts/*.md) and config/research.yaml are read on every use and need nothing here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger(__name__)

OnChange = Callable[[], Awaitable[None]]


class ConfigWatcher:
    def __init__(self, interval: float = 30):
        self.interval = interval
        self._watched: dict[Path, tuple[float | None, OnChange]] = {}
        self._task: asyncio.Task | None = None

    def watch(self, path: Path, on_change: OnChange) -> None:
        self._watched[path] = (self._mtime(path), on_change)

    @staticmethod
    def _mtime(path: Path) -> float | None:
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return None

    async def check(self) -> list[Path]:
        changed = []
        for path, (seen, on_change) in list(self._watched.items()):
            now = self._mtime(path)
            if now == seen:
                continue
            self._watched[path] = (now, on_change)
            changed.append(path)
            try:
                await on_change()
                log.info("reloaded %s", path.name)
            except Exception:
                log.exception("reloading %s failed; keeping the previous config", path.name)
        return changed

    def start(self) -> None:
        async def loop() -> None:
            while True:
                await asyncio.sleep(self.interval)
                await self.check()

        self._task = asyncio.create_task(loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
