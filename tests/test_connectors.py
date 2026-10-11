import httpx
import pytest

from agent.db import Database
from agent.sources import connectors
from agent.sources.connectors import Company, detect_ats, fetch_company_jobs, poll_ats, poll_devpost, poll_hn_hiring, poll_jobtech
from agent.sources.pages import make_page_poller, page_text
from agent.sources.streams import StreamConfig, StreamItem


def client(routes: dict[str, object]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, body in routes.items():
            if str(request.url).startswith(prefix):
                if isinstance(body, str):
                    return httpx.Response(200, text=body)
                return httpx.Response(200, json=body)
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def cfg(type_, **params) -> StreamConfig:
    return StreamConfig(id="t", type=type_, url="https://page.example/", params=params)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


async def test_jobtech_maps_and_dedupes_across_queries():
    hit = {"id": "1", "headline": "Junior utvecklare", "employer": {"name": "FOI"}, "webpage_url": "https://af/1",
           "application_deadline": "2026-11-01T23:59:59", "workplace_address": {"municipality": "Stockholm"},
           "description": {"text": "<p>Kul</p>"}}
    async with client({"https://jobsearch.api.jobtechdev.se/search": {"hits": [hit]}}) as c:
        items = await poll_jobtech(cfg("jobtech", queries=["junior", "student"], occupation_field="x"), c)
    assert len(items) == 1
    assert items[0].title == "Junior utvecklare – FOI" and items[0].raw["deadline"] == "2026-11-01"


async def test_devpost_filters_on_location():
    body = {"hackathons": [
        {"id": 1, "title": "AI Hack", "url": "https://d/1", "displayed_location": {"location": "Online"}, "themes": [{"name": "AI"}], "prize_amount": "$<span>10,000</span>", "submission_period_dates": "Oct 1 - Nov 1"},
        {"id": 2, "title": "Local NYC", "url": "https://d/2", "displayed_location": {"location": "New York"}, "themes": []},
    ]}
    async with client({"https://devpost.com/api/hackathons": body}) as c:
        items = await poll_devpost(cfg("devpost", locations=["Online", "Stockholm"], pages=1), c)
    assert [i.title for i in items] == ["AI Hack (Online)"]
    assert "10,000" in items[0].text and "<span>" not in items[0].text


async def test_ats_watchlist_whole_word_roles(tmp_path, monkeypatch):
    wl = tmp_path / "w.yaml"
    wl.write_text("companies:\n  - {name: Spotify, ats: lever, slug: spotify}\n  - {name: X, careers_url: https://x}\n")
    monkeypatch.setattr(connectors, "WATCHLIST", wl)
    postings = [
        {"id": "a", "text": "Summer Internship 2027", "hostedUrl": "https://l/a", "categories": {"location": "Stockholm"}},
        {"id": "b", "text": "Internal Communications Lead", "hostedUrl": "https://l/b", "categories": {"location": "Stockholm"}},
        {"id": "c", "text": "Summer Internship 2027", "hostedUrl": "https://l/c", "categories": {"location": "New York"}},
    ]
    async with client({"https://api.lever.co/v0/postings/spotify": postings}) as c:
        items = await poll_ats(cfg("ats", locations=["Stockholm"], roles=["internship", "intern"]), c)
    assert [i.key for i in items] == ["lever:spotify:a"]


async def test_fetch_company_jobs_per_ats():
    routes = {
        "https://boards-api.greenhouse.io/v1/boards/g/jobs": {"jobs": [{"id": 1, "title": "Intern", "location": {"name": "Stockholm"}, "absolute_url": "https://g/1"}]},
        "https://api.ashbyhq.com/posting-api/job-board/a": {"jobs": [{"id": "x", "title": "Intern", "location": "Remote", "jobUrl": "https://a/x"}]},
        "https://careers.example.com/jobs.rss": "<rss><channel><item><title>Intern</title><link>https://t/1</link><guid>t1</guid></item></channel></rss>",
    }
    async with client(routes) as c:
        assert (await fetch_company_jobs(Company("G", "greenhouse", "g"), c))[0][1] == "Intern"
        assert (await fetch_company_jobs(Company("A", "ashby", "a"), c))[0][3] == "https://a/x"
        assert (await fetch_company_jobs(Company("T", "teamtailor", "careers.example.com"), c))[0][0] == "t1"


async def test_detect_ats_from_careers_page():
    async with client({"https://co.example/careers": '<a href="https://jobs.lever.co/acme/123">Jobs</a>'}) as c:
        assert await detect_ats("https://co.example/careers", c) == ("lever", "acme")
    async with client({"https://co.example/careers": '<script src="https://app.teamtailor.com/x.js"></script>'}) as c:
        # app.teamtailor.com is Teamtailor's own host, not the company's board -> falls back to the page host
        assert await detect_ats("https://co.example/careers", c) == ("teamtailor", "co.example")


async def test_hn_hiring_filters_comments():
    routes = {
        "https://hn.algolia.com/api/v1/search_by_date": {"hits": [{"title": "Ask HN: Who is hiring? (October 2026)", "objectID": "9"}]},
        "https://hn.algolia.com/api/v1/items/9": {"children": [
            {"id": 1, "text": "Lovable | Stockholm | AI engineers"},
            {"id": 2, "text": "Acme | San Francisco | Backend"},
        ]},
    }
    async with client(routes) as c:
        items = await poll_hn_hiring(cfg("hn_hiring", include=["Stockholm"]), c)
    assert [i.title for i in items] == ["Lovable"]


def test_page_text_keeps_links_and_drops_scripts():
    lines = page_text('<html><head><title>x</title></head><body><script>var a=1</script>'
                      '<li><a href="/e/1">Career fair</a> 12 Nov</li><p>Hi there</p></body></html>', "https://ths.example/events")
    assert "Career fair [https://ths.example/e/1] 12 Nov" in lines and not any("var a" in line for line in lines)


async def test_page_poller_only_extracts_when_page_changes(db):
    seen_lines = []

    async def extract(config, lines):
        seen_lines.append(lines)
        return [StreamItem(key=line[:8], title=line) for line in lines if "Event" in line]

    poller = make_page_poller(db, extract)
    page = {"v": "<li>Event A</li><li>Footer stuff</li>"}

    def handler(request):
        return httpx.Response(200, text=page["v"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        c1 = StreamConfig(id="p", type="page", url="https://p/")
        assert [i.title for i in await poller(c1, c)] == ["Event A"]
        assert await poller(c1, c) == []  # unchanged -> no Claude call
        page["v"] = "<li>Event A</li><li>Event B</li><li>Footer stuff</li>"
        assert [i.title for i in await poller(c1, c)] == ["Event B"]  # only the new line
    assert len(seen_lines) == 2 and seen_lines[1] == ["Event B"]


async def test_topic_combines_news_and_hn():
    from agent.sources.connectors import poll_topic

    rss = "<rss><channel><item><title>Ny inkubator</title><link>https://n/1</link><guid>g1</guid></item></channel></rss>"
    routes = {
        "https://news.google.com/rss/search": rss,
        "https://hn.algolia.com/api/v1/search_by_date": {"hits": [{"objectID": "9", "title": "Show HN: X", "url": "https://x", "points": 5}]},
    }
    async with client(routes) as c:
        items = await poll_topic(cfg("topic", query="KTH Innovation", lang="sv"), c)
    assert [i.key for i in items] == ["news:g1", "hn:9"] and items[1].url == "https://x"
