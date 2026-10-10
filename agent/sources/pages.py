"""Page watcher: any web page as a sense. The page is reduced to text; when it changes, Haiku
reads the new lines and lists the new items (events, jobs, news) as StreamItems.

Costs nothing while the page is unchanged.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import httpx

from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.db import Database
from agent.sources.streams import StreamConfig, StreamItem

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": ["string", "null"]},
                    "summary": {"type": "string"},
                    "date": {"type": ["string", "null"], "description": "YYYY-MM-DD om det finns"},
                },
                "required": ["title", "summary"],
            },
        }
    },
    "required": ["items"],
}
PROMPT = """Här är nya rader från webbsidan {url} ({note}).
Lista de nya, konkreta sakerna som raderna beskriver: event, jobb, utlysningar, nyheter. Ta inte med navigering,
sidfötter, cookie-text eller sådant som bara flyttats runt. Använd länkarna i [hakparentes] när de hör till en sak.
Hitta inte på något som inte står där. Inga nya saker ger en tom lista.

Nya rader:
{lines}
"""

Extract = Callable[[StreamConfig, list[str]], Awaitable[list[StreamItem]]]


def page_text(markup: str, base: str) -> list[str]:
    """HTML -> meaningful lines, with link targets kept inline as [url]."""
    markup = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", markup)
    markup = re.sub(r'(?is)<a\b[^>]*href="([^"#][^"]*)"[^>]*>(.*?)</a>', lambda m: f"{m.group(2)} [{urljoin(base, m.group(1))}]", markup)
    markup = re.sub(r"(?i)<(br|/p|/div|/li|/h\d|/tr|/article|/section)\b[^>]*>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return [line for line in lines if len(line) > 3]


def claude_extractor(gate: ClaudeGate, model: str) -> Extract:
    async def extract(config: StreamConfig, lines: list[str]) -> list[StreamItem]:
        text = "\n".join(lines)[:12000]
        try:
            r = await gate.run(
                ClaudeTask(
                    kind="page_extract",
                    prompt=PROMPT.format(url=config.url, note=config.note or "ingen beskrivning", lines=text),
                    priority=Priority.BACKGROUND,
                    model=model,
                    output_schema=SCHEMA,
                    max_turns=2,
                )
            )
        except QuotaPaused:
            raise RuntimeError("quota paused")  # the poller keeps the old snapshot and retries next time
        if r.is_error or not isinstance(r.structured, dict):
            raise RuntimeError(r.error or "no structured output")
        out = []
        for it in r.structured.get("items", []):
            key = hashlib.sha1(f"{it['title']}|{it.get('url') or ''}".encode()).hexdigest()[:16]
            out.append(
                StreamItem(
                    key=key,
                    title=it["title"][:200],
                    url=it.get("url") or config.url,
                    text=it.get("summary", "") + (f" Datum: {it['date']}." if it.get("date") else ""),
                )
            )
        return out

    return extract


def make_page_poller(db: Database, extract: Extract):
    async def poll_page(config: StreamConfig, client: httpx.AsyncClient) -> list[StreamItem]:
        r = await client.get(config.url)
        r.raise_for_status()
        lines = page_text(r.text, str(r.url))
        key = f"page.snapshot.{config.id}"
        previous = set(json.loads(await db.kv_get(key, "[]")))
        digest = hashlib.sha1("\n".join(lines).encode()).hexdigest()
        if await db.kv_get(f"{key}.hash") == digest:
            return []
        new_lines = [line for line in lines if line not in previous] if previous else lines
        items = await extract(config, new_lines) if new_lines else []
        # Only remember the snapshot once extraction succeeded, so a failure is retried next poll.
        await db.kv_set(key, json.dumps(lines))
        await db.kv_set(f"{key}.hash", digest)
        return items

    return poll_page
