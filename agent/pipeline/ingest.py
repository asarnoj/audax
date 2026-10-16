"""Ingest: stream items -> prefilter -> events -> (debounced) triage -> dispatcher.

Items are batched for a short while before triage, so a burst of notifications costs one
Claude run instead of one per item.
"""

from __future__ import annotations

from agent import trail

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from agent.db import Database
from agent.sources.connectors import POLLERS
from agent.sources.pages import Extract, make_page_poller
from agent.sources.streams import StreamConfig, StreamItem, load_stream_configs, run_poller, run_websocket

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 120


class Ingest:
    def __init__(
        self,
        db: Database,
        process: Callable[[], Awaitable[None]],
        debounce: float = DEBOUNCE_SECONDS,
        extract: Extract | None = None,  # Claude extraction for page watchers
    ):
        self.db = db
        self._pollers = dict(POLLERS)
        if extract is not None:
            self._pollers["page"] = make_page_poller(db, extract)
        self._process = process  # triage + dispatcher
        self._debounce = debounce
        self._wake = asyncio.Event()
        self._streams: list[asyncio.Task] = []
        self._loop_task: asyncio.Task | None = None

    async def emit(self, config: StreamConfig, items: list[StreamItem], baseline: bool = False) -> int:
        """Stores new items as events. Baseline items are marked as already seen (no flood on first start)."""
        new = 0
        for item in items:
            if not config.accepts(item):
                continue
            payload = {"title": item.title, "url": item.url, "summary": item.text, "published": item.published}
            if item.raw.get("deadline"):
                payload["deadline"] = item.raw["deadline"]
            if config.note:
                payload["source_note"] = config.note
            new += bool(await self.db.insert("events", ignore=True, source=config.source, kind=config.kind, dedupe_key=item.key,
                                             payload=json.dumps(payload, ensure_ascii=False), triaged_at="baseline" if baseline else None))
        if new and not baseline:
            await trail.emit("print", "director", n=new)   # the printer on Director's desk
        await self._stats(config.id, fetched=len(items), new=0 if baseline else new)
        if new and not baseline:
            self._wake.set()
        return new

    async def _stats(self, stream: str, fetched: int = 0, new: int = 0, error: str | None = None) -> None:
        await self.db.execute(
            "INSERT INTO stream_stats (stream, polls, fetched, new, errors, last_poll, last_new, last_error) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'), CASE WHEN ? > 0 THEN datetime('now') END, ?) "
            "ON CONFLICT(stream) DO UPDATE SET polls = polls + excluded.polls, fetched = fetched + excluded.fetched, "
            "new = new + excluded.new, errors = errors + excluded.errors, last_poll = excluded.last_poll, "
            "last_new = COALESCE(excluded.last_new, last_new), last_error = COALESCE(excluded.last_error, last_error)",
            (stream, 0 if error else 1, fetched, new, 1 if error else 0, new, error),
        )

    async def record_error(self, config: StreamConfig, error: Exception) -> None:
        await self._stats(config.id, error=f"{type(error).__name__}: {error}"[:200])

    async def start(self, configs: list[StreamConfig] | None = None) -> None:
        await self._start_streams(load_stream_configs() if configs is None else configs)
        self._loop_task = asyncio.create_task(self._loop())
        if await self.db.fetchone("SELECT 1 FROM events WHERE triaged_at IS NULL LIMIT 1"):
            self._wake.set()  # events left untriaged by a restart

    async def restart_streams(self) -> None:
        """Called when config/sources.yaml changes."""
        await self._stop_streams()
        await self._start_streams(load_stream_configs())

    async def _start_streams(self, configs: list[StreamConfig]) -> None:
        for c in configs:
            if not c.enabled:
                continue
            first = c.baseline and not await self.db.kv_get(f"stream.started.{c.id}", False)
            await self.db.kv_set(f"stream.started.{c.id}", True)
            if c.type in self._pollers:
                self._streams.append(asyncio.create_task(
                    run_poller(c, self.emit, self._pollers[c.type], first_poll_is_baseline=first, on_error=self.record_error)
                ))
            elif c.type == "websocket":
                self._streams.append(asyncio.create_task(run_websocket(c, self.emit)))
            else:
                log.warning("stream %s: unknown type %s", c.id, c.type)
        log.info("streams running: %s", ", ".join(c.id for c in configs if c.enabled) or "none")

    async def _stop_streams(self) -> None:
        for t in self._streams:
            t.cancel()
        await asyncio.gather(*self._streams, return_exceptions=True)
        self._streams = []

    async def stop(self) -> None:
        await self._stop_streams()
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await self._wake.wait()
            await asyncio.sleep(self._debounce)  # let a burst accumulate
            self._wake.clear()
            try:
                await self._process()
            except Exception:
                log.exception("ingest processing failed")
