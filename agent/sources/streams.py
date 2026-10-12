"""Event streams: sources that push (WebSocket) or publish feeds (RSS/Atom) into the event pipeline.

Each stream is configured in config/sources.yaml under `streams`. Adding a new one is config only:

    - id: hn-best                 # unique, becomes source "stream:hn-best"
      type: rss | websocket
      url: ...                    # or url_env: NAME_IN_DOTENV for secret URLs
      kind: news                  # event kind passed to triage
      note: "..."                 # hint for triage about what matters from this source
      filter: {include: [...], exclude: [...]}   # cheap keyword prefilter, case-insensitive
      # rss:       poll_minutes: 30
      # websocket: subscribe: '<json sent after connect>'
      #            fields: {id: "a.b", title: "...", url: "...", text: "..."}   # dotted paths into each JSON message
"""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import feedparser
import httpx
import yaml

from agent.netguard import public_client
from agent.config import instance_config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamItem:
    key: str  # dedupe key within the source
    title: str
    url: str = ""
    text: str = ""
    published: str | None = None  # ISO 8601
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StreamConfig:
    id: str
    type: str
    url: str
    kind: str = "news"
    note: str = ""
    enabled: bool = True
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    title_only: bool = False  # prefilter on the title only (job ads mention "junior" etc. in boilerplate)
    poll_minutes: float = 30
    subscribe: str | None = None
    fields: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)  # type-specific, see agent/sources/connectors.py
    baseline: bool = True  # first poll only records what exists (news); False = triage what's already there (jobs)

    @property
    def source(self) -> str:
        return f"stream:{self.id}"

    def accepts(self, item: StreamItem) -> bool:
        """Keyword prefilter: no LLM involved, so it costs nothing."""
        hay = item.title.lower() if self.title_only else f"{item.title}\n{item.text}".lower()
        if self.exclude and any(w.lower() in hay for w in self.exclude):
            return False
        return not self.include or any(w.lower() in hay for w in self.include)


URL_REQUIRED = ("rss", "websocket", "page")
USER_AGENT = "Mozilla/5.0 (compatible; llm-agent/1.0; personal assistant)"


class Emit(Protocol):
    async def __call__(self, config: StreamConfig, items: list[StreamItem], baseline: bool = False) -> int: ...


CONFIG_ERRORS: dict[str, str] = {}  # file -> problem, reported by the health watch


def load_stream_configs() -> list[StreamConfig]:
    """Never fatal: a broken sources.yaml means no streams (and a health alert), not a crash loop."""
    try:
        data = (yaml.safe_load(instance_config("sources.yaml").read_text()) or {}).get("streams") or []
        CONFIG_ERRORS.pop("sources.yaml", None)
    except Exception as e:
        CONFIG_ERRORS["sources.yaml"] = f"{type(e).__name__}: {str(e)[:150]}"
        log.error("sources.yaml unreadable, running without streams: %s", e)
        return []
    out = []
    for s in data:
        url = s.get("url") or os.getenv(s.get("url_env", ""), "").strip()
        f = s.get("filter") or {}
        out.append(
            StreamConfig(
                id=s["id"],
                type=s["type"],
                url=url,
                kind=s.get("kind", "news"),
                note=" ".join(str(s.get("note", "")).split()),
                enabled=s.get("enabled", True) and (bool(url) or s["type"] not in URL_REQUIRED),
                include=tuple(f.get("include") or ()),
                exclude=tuple(f.get("exclude") or ()),
                title_only=bool(f.get("title_only", False)),
                poll_minutes=float(s.get("poll_minutes", 30)),
                subscribe=s.get("subscribe"),
                fields=s.get("fields") or {},
                params=s.get("params") or {},
                baseline=s.get("baseline", True),
            )
        )
    return out


# --- RSS / Atom and the polling loop -------------------------------------------


def parse_feed(content: bytes | str) -> list[StreamItem]:
    feed = feedparser.parse(content)
    items = []
    for e in feed.entries:
        title = (e.get("title") or "").strip()
        if not title:
            continue
        published = None
        if e.get("published_parsed"):
            published = datetime.fromtimestamp(calendar.timegm(e.published_parsed), tz=timezone.utc).isoformat()
        text = re.sub(r"<[^>]+>", " ", e.get("summary") or "")
        items.append(
            StreamItem(
                key=e.get("id") or e.get("link") or title,
                title=title,
                url=e.get("link") or "",
                text=" ".join(text.split())[:1500],
                published=published,
            )
        )
    return items


async def run_poller(config: StreamConfig, emit: Emit, poller, first_poll_is_baseline: bool, on_error=None) -> None:
    """Generic loop for every polled sense (rss, jobtech, ats, page, ...)."""
    baseline = first_poll_is_baseline
    while True:
        try:
            async with public_client(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
                items = await poller(config, client)
            n = await emit(config, items, baseline=baseline)
            if n or baseline:
                log.info("stream %s: %d new item(s)%s", config.id, n, " (baseline)" if baseline else "")
            baseline = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("stream %s poll failed: %s", config.id, e)
            if on_error:
                try:
                    await on_error(config, e)
                except Exception:
                    pass
        await asyncio.sleep(config.poll_minutes * 60)


# --- WebSocket --------------------------------------------------------------


def dig(obj: Any, path: str) -> Any:
    for part in path.split(".") if path else []:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def ws_item(config: StreamConfig, message: str | bytes) -> StreamItem | None:
    try:
        data = json.loads(message)
    except (ValueError, TypeError):
        return None
    f = config.fields
    title = dig(data, f.get("title", "title"))
    if not title:
        return None
    url = dig(data, f.get("url", "url")) or ""
    key = dig(data, f.get("id", "id")) or url or title
    text = dig(data, f.get("text", "text")) or ""
    return StreamItem(key=str(key), title=str(title)[:300], url=str(url), text=str(text)[:1500], raw=data if len(message) < 4000 else {})


async def run_websocket(config: StreamConfig, emit: Emit) -> None:
    from websockets.asyncio.client import connect

    backoff = 5
    while True:
        try:
            async with connect(config.url, open_timeout=30, ping_interval=30) as ws:
                log.info("stream %s connected", config.id)
                if config.subscribe:
                    await ws.send(config.subscribe)
                backoff = 5
                async for message in ws:
                    item = ws_item(config, message)
                    if item is not None:
                        await emit(config, [item])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("stream %s disconnected (%s), reconnecting in %ss", config.id, e, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 600)
