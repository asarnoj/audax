import httpx
import pytest

from agent.db import Database
from agent.web import WebReader

HTML = "<html><body>" + "<p>Banana Hacks rules: free to enter, teams of 1-4.</p>" * 20 + "</body></html>"


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


def transport(routes):
    def handler(req: httpx.Request) -> httpx.Response:
        for prefix, resp in routes.items():
            if str(req.url).startswith(prefix):
                return resp() if callable(resp) else resp
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_devpost_uses_api_and_page(db):
    t = transport({
        "https://devpost.com/api/hackathons": httpx.Response(200, json={"hackathons": [{
            "title": "Banana Hacks", "url": "https://banana-hacks-1111.devpost.com/", "open_state": "open",
            "submission_period_dates": "Oct 09 - 12, 2026", "registrations_count": 465, "prize_amount": "$<span>500</span>",
            "themes": [{"name": "AI"}], "displayed_location": {"location": "Online"}}]}),
        "https://banana-hacks-1111.devpost.com/rules": httpx.Response(200, text=HTML.replace("Banana Hacks rules", "Official rules"), headers={"content-type": "text/html"}),
        "https://banana-hacks-1111.devpost.com": httpx.Response(200, text=HTML, headers={"content-type": "text/html"}),
    })
    text, method = await WebReader(db, transport=t).read("https://banana-hacks-1111.devpost.com/")
    assert method == "api" and "Status: open" in text and "465" in text and "teams of 1-4" in text and "[Regler]" in text


async def test_falls_back_to_reader_when_blocked_and_remembers(db):
    calls = []

    def blocked():
        calls.append("direct")
        return httpx.Response(403, text="Just a moment...", headers={"content-type": "text/html"})

    t = transport({
        "https://r.jina.ai/": httpx.Response(200, text="Title: Event\n\n" + "Innehåll " * 50),
        "https://site.example/": blocked,
    })
    w = WebReader(db, transport=t)
    text, method = await w.read("https://site.example/event")
    assert method == "reader" and "Innehåll" in text
    await w.read("https://site.example/other")
    assert calls == ["direct"]  # second time the reader is tried first for this domain
    assert "site.example" in await w.stats()


async def test_reports_failure_honestly_without_reader(db):
    t = transport({"https://x.example/": httpx.Response(503, text="down")})
    text, method = await WebReader(db, transport=t, reader_service=False).read("x.example/page")
    assert method == "none" and text.startswith("Kunde inte läsa https://x.example/page")
