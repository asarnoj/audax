import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from agent.db import Database
from agent.pipeline.ingest import Ingest
from agent.sources.streams import StreamConfig, StreamItem, dig, parse_feed, run_websocket, ws_item

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>New AI model released</title><link>https://a.example/1</link><guid>g1</guid>
<description><![CDATA[<p>Big <b>news</b></p>]]></description><pubDate>Fri, 09 Oct 2026 17:00:00 +0000</pubDate></item>
<item><title>Bank earnings beat</title><link>https://a.example/2</link><guid>g2</guid></item>
</channel></rss>"""


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


def cfg(**kw) -> StreamConfig:
    return StreamConfig(**{"id": "s", "type": "rss", "url": "x", **kw})


def test_parse_feed_strips_html_and_reads_dates():
    items = parse_feed(RSS)
    assert [i.key for i in items] == ["g1", "g2"]
    assert items[0].text == "Big news"
    assert items[0].published.startswith("2026-10-09T17:00")


def test_prefilter_include_exclude():
    a = StreamItem(key="1", title="New AI model released")
    b = StreamItem(key="2", title="Bank earnings beat")
    assert cfg(exclude=("bank",)).accepts(a) and not cfg(exclude=("bank",)).accepts(b)
    assert cfg(include=("ai",)).accepts(a) and not cfg(include=("ai",)).accepts(b)
    assert cfg().accepts(b)


def test_ws_item_mapping_and_dig():
    c = cfg(type="websocket", fields={"id": "data.id", "title": "data.headline", "url": "data.link"})
    item = ws_item(c, json.dumps({"data": {"id": 7, "headline": "Hackathon announced", "link": "https://h"}}))
    assert (item.key, item.title, item.url) == ("7", "Hackathon announced", "https://h")
    assert ws_item(c, "not json") is None
    assert ws_item(c, json.dumps({"data": {}})) is None  # no title -> ignored
    assert dig({"a": [{"b": 1}]}, "a.0.b") == 1


async def test_ingest_dedupes_baselines_and_batches(db):
    processed = []

    async def process():
        processed.append(1)

    ingest = Ingest(db, process, debounce=0.05)
    loop = asyncio.create_task(ingest._loop())
    c = cfg(note="bara stora saker")
    items = parse_feed(RSS)

    assert await ingest.emit(c, items, baseline=True) == 2
    await asyncio.sleep(0.1)
    assert processed == []  # baseline never triggers triage

    new = StreamItem(key="g3", title="Third")
    assert await ingest.emit(c, items + [new]) == 1  # only the new one
    assert await ingest.emit(c, [StreamItem(key="g4", title="Fourth")]) == 1
    await asyncio.sleep(0.2)
    assert processed == [1]  # two emits within the debounce window -> one processing run

    rows = await db.fetchall("SELECT dedupe_key, triaged_at, payload FROM events ORDER BY id")
    assert [(r["dedupe_key"], r["triaged_at"]) for r in rows] == [("g1", "baseline"), ("g2", "baseline"), ("g3", None), ("g4", None)]
    assert json.loads(rows[2]["payload"])["source_note"] == "bara stora saker"
    loop.cancel()


async def test_websocket_end_to_end(db):
    received_subscribe = asyncio.Event()

    async def handler(ws):
        msg = await ws.recv()
        assert json.loads(msg) == {"action": "subscribe"}
        received_subscribe.set()
        await ws.send(json.dumps({"id": "a", "title": "Live event", "url": "https://live"}))
        await ws.send("garbage")
        await asyncio.sleep(1)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        emitted = []

        async def emit(config, items, baseline=False):
            emitted.extend(items)
            return len(items)

        c = cfg(type="websocket", url=f"ws://127.0.0.1:{port}", subscribe='{"action": "subscribe"}')
        task = asyncio.create_task(run_websocket(c, emit))
        for _ in range(50):
            if emitted:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert received_subscribe.is_set()
    assert [i.title for i in emitted] == ["Live event"]
