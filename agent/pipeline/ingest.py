"""Ingest: stream items -> prefilter -> events -> (debounced) triage -> dispatcher.

Items are batched for a short while before triage, so a burst of notifications costs one
Claude run instead of one per item.
"""

from __future__ import annotations

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
            cur = await self.db.conn.execute(
                "INSERT OR IGNORE INTO events (source, kind, dedupe_key, payload, triaged_at) VALUES (?, ?, ?, ?, ?)",
                (config.source, config.kind, item.key, json.dumps(payload, ensure_ascii=False), "baseline" if baseline else None),
            )
            new += cur.rowcount
        await self.db.conn.commit()
        if new and not baseline:
            self._wake.set()
        return new

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
                self._streams.append(asyncio.create_task(run_poller(c, self.emit, self._pollers[c.type], first_poll_is_baseline=first)))
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
