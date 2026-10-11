"""Structured senses: pollers that enumerate a source completely (docs/SENSES.md, layer 1-2).

Each poller is `async (config, client) -> list[StreamItem]` and is registered in POLLERS.
Type-specific settings live in the stream's `params`.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from agent.config import instance_config
from agent.sources.streams import StreamConfig, StreamItem, parse_feed

log = logging.getLogger(__name__)

Poller = Callable[[StreamConfig, httpx.AsyncClient], Awaitable[list[StreamItem]]]
WATCHLIST = instance_config("watchlist.yaml")


def _clean(text: str, limit: int = 1500) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return " ".join(text.split())[:limit]


def _matches(text: str, words: list[str]) -> bool:
    t = text.lower()
    return not words or any(w.lower() in t for w in words)


def _matches_words(text: str, words: list[str]) -> bool:
    """Whole-word match, so "intern" doesn't match "Internal"."""
    return not words or any(re.search(rf"\b{re.escape(w.lower())}\b", text.lower()) for w in words)


# --- RSS (also used by the generic loop) ------------------------------------


async def poll_rss(config: StreamConfig, client: httpx.AsyncClient) -> list[StreamItem]:
    r = await client.get(config.url)
    r.raise_for_status()
    return parse_feed(r.content)


# --- JobTech: Arbetsförmedlingen's open job search API -------------------------


async def poll_jobtech(config: StreamConfig, client: httpx.AsyncClient) -> list[StreamItem]:
    """params: queries, occupation_field / region_id (JobTech taxonomy concept ids), region (free-text
    match on workplace), limit per query."""
    p = config.params
    items: dict[str, StreamItem] = {}
    for q in p.get("queries", []):
        query = {"q": q, "limit": int(p.get("limit", 50))}
        if p.get("occupation_field"):
            query["occupation-field"] = p["occupation_field"]
        if p.get("region_id"):
            query["region"] = p["region_id"]
        r = await client.get("https://jobsearch.api.jobtechdev.se/search", params=query, headers={"accept": "application/json"})
        r.raise_for_status()
        for h in r.json().get("hits", []):
            place = " ".join(filter(None, [(h.get("workplace_address") or {}).get(k) for k in ("municipality", "region")]))
            if p.get("region") and not _matches(place, p["region"] if isinstance(p["region"], list) else [p["region"]]):
                continue
            employer = (h.get("employer") or {}).get("name") or ""
            deadline = (h.get("application_deadline") or "")[:10]
            items[str(h["id"])] = StreamItem(
                key=str(h["id"]),
                title=f"{h.get('headline', '')} – {employer}".strip(" –"),
                url=h.get("webpage_url") or "",
                text=f"{place}. Sista ansökan {deadline}. " + _clean((h.get("description") or {}).get("text", ""), 1000),
                published=h.get("publication_date"),
                raw={"deadline": deadline or None, "query": q},
            )
    return list(items.values())


# --- Devpost hackathons ---------------------------------------------------------


async def poll_devpost(config: StreamConfig, client: httpx.AsyncClient) -> list[StreamItem]:
    """params: locations (match on displayed location, e.g. Online, Stockholm, Sweden), pages."""
    p = config.params
    items = []
    for page in range(1, int(p.get("pages", 3)) + 1):
        r = await client.get(
            "https://devpost.com/api/hackathons", params=[("status[]", "upcoming"), ("status[]", "open"), ("page", page)]
        )
        r.raise_for_status()
        for h in r.json().get("hackathons", []):
            location = (h.get("displayed_location") or {}).get("location", "")
            if not _matches(location, p.get("locations", [])):
                continue
            themes = ", ".join(t["name"] for t in h.get("themes", []))
            prize = _clean(h.get("prize_amount", ""), 60)
            items.append(
                StreamItem(
                    key=str(h["id"]),
                    title=f"{h['title']} ({location})",
                    url=h.get("url", ""),
                    text=f"{h.get('submission_period_dates', '')}. Teman: {themes}. Priser: {prize}.",
                )
            )
    return items


# --- Company watchlist via applicant tracking systems ----------------------------


@dataclass(frozen=True)
class Company:
    name: str
    ats: str  # lever|greenhouse|ashby|smartrecruiters|teamtailor
    slug: str  # board slug, or for teamtailor the careers host


def load_watchlist(path: Path | None = None) -> list[Company]:
    path = path or WATCHLIST
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [Company(c["name"], c["ats"], c["slug"]) for c in data.get("companies", []) if c.get("ats") and c.get("slug")]


async def fetch_company_jobs(c: Company, client: httpx.AsyncClient) -> list[tuple[str, str, str, str]]:
    """Returns (id, title, location, url) for every open posting."""
    if c.ats == "lever":
        r = await client.get(f"https://api.lever.co/v0/postings/{c.slug}", params={"mode": "json"})
        r.raise_for_status()
        return [(p["id"], p["text"], (p.get("categories") or {}).get("location") or "", p["hostedUrl"]) for p in r.json()]
    if c.ats == "greenhouse":
        r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{c.slug}/jobs")
        r.raise_for_status()
        return [(str(j["id"]), j["title"], (j.get("location") or {}).get("name", ""), j["absolute_url"]) for j in r.json()["jobs"]]
    if c.ats == "ashby":
        r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{c.slug}")
        r.raise_for_status()
        return [(j["id"], j["title"], j.get("location") or "", j.get("jobUrl", "")) for j in r.json().get("jobs", [])]
    if c.ats == "smartrecruiters":
        r = await client.get(f"https://api.smartrecruiters.com/v1/companies/{c.slug}/postings", params={"limit": 100})
        r.raise_for_status()
        return [
            (j["id"], j["name"], (j.get("location") or {}).get("city", ""), f"https://jobs.smartrecruiters.com/{c.slug}/{j['id']}")
            for j in r.json().get("content", [])
        ]
    if c.ats == "teamtailor":
        r = await client.get(f"https://{c.slug}/jobs.rss")
        r.raise_for_status()
        return [(i.key, i.title, "", i.url) for i in parse_feed(r.content)]
    raise ValueError(f"unknown ats {c.ats}")


async def poll_ats(config: StreamConfig, client: httpx.AsyncClient) -> list[StreamItem]:
    """Every company on the watchlist. params: locations, roles (keywords in the title; empty = all)."""
    p = config.params
    items = []
    for c in load_watchlist():
        try:
            jobs = await fetch_company_jobs(c, client)
        except Exception as e:
            log.warning("watchlist %s (%s) failed: %s", c.name, c.ats, e)
            continue
        for jid, title, location, url in jobs:
            if not _matches(location or title, p.get("locations", [])) or not _matches_words(title, p.get("roles", [])):
                continue
            items.append(StreamItem(key=f"{c.ats}:{c.slug}:{jid}", title=f"{title} – {c.name}", url=url, text=f"{c.name}, {location}"))
    return items


ATS_PATTERNS = [
    ("lever", r"jobs\.lever\.co/([\w-]+)"),
    ("greenhouse", r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:v1/boards/)?([\w-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([\w.-]+)"),
    ("smartrecruiters", r"(?:careers|jobs)\.smartrecruiters\.com/([\w-]+)"),
    ("teamtailor", r"https?://((?!app\.|www\.|assets\.|scripts\.)[\w.-]+\.teamtailor\.com)"),
]


async def detect_ats(careers_url: str, client: httpx.AsyncClient) -> tuple[str, str] | None:
    """Finds which applicant tracking system a careers page uses: (ats, slug) or None."""
    r = await client.get(careers_url)
    page = f"{r.url} {r.text}"
    for ats, pattern in ATS_PATTERNS:
        if m := re.search(pattern, page):
            return ats, m.group(1)
    if "teamtailor" in page.lower():  # Teamtailor on a custom domain
        return "teamtailor", httpx.URL(str(r.url)).host
    return None


# --- HN "Who is hiring" ---------------------------------------------------------


async def poll_hn_hiring(config: StreamConfig, client: httpx.AsyncClient) -> list[StreamItem]:
    """Top-level comments in this month's thread. params: include (any of, e.g. Stockholm, Sweden, Europe remote)."""
    r = await client.get(
        "https://hn.algolia.com/api/v1/search_by_date", params={"tags": "story,author_whoishiring", "hitsPerPage": 5}
    )
    r.raise_for_status()
    story = next((h for h in r.json()["hits"] if "who is hiring" in h["title"].lower()), None)
    if story is None:
        return []
    r = await client.get(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")
    r.raise_for_status()
    items = []
    for c in r.json().get("children", []):
        text = _clean(c.get("text") or "", 2000)
        if not text or not _matches(text, config.params.get("include", [])):
            continue
        items.append(
            StreamItem(
                key=str(c["id"]),
                title=text.split("|")[0].strip()[:120] or text[:120],
                url=f"https://news.ycombinator.com/item?id={c['id']}",
                text=text,
            )
        )
    return items


# --- Topics: follow news about something specific ----------------------------------


def _news_url(query: str, lang: str) -> str:
    from urllib.parse import quote

    hl, gl, ceid = ("sv", "SE", "SE:sv") if lang == "sv" else ("en-US", "US", "US:en")
    return f"https://news.google.com/rss/search?q={quote(query)}+when:7d&hl={hl}&gl={gl}&ceid={ceid}"


async def poll_topic(config: StreamConfig, client: httpx.AsyncClient) -> list[StreamItem]:
    """News (Google News search feed) and Hacker News about a topic.
    params: query, lang (sv|en), sources ([news, hn])."""
    p = config.params
    query, items = p["query"], []
    sources = p.get("sources") or ["news", "hn"]
    if "news" in sources:
        r = await client.get(_news_url(query, p.get("lang", "en")))
        r.raise_for_status()
        items += [StreamItem(key=f"news:{i.key}", title=i.title, url=i.url, text=i.text, published=i.published) for i in parse_feed(r.content)]
    if "hn" in sources:
        r = await client.get(
            "https://hn.algolia.com/api/v1/search_by_date", params={"query": query, "tags": "story", "hitsPerPage": 20}
        )
        r.raise_for_status()
        items += [
            StreamItem(
                key=f"hn:{h['objectID']}",
                title=h["title"],
                url=h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                text=f"Hacker News, {h.get('points', 0)} poäng, {h.get('num_comments', 0)} kommentarer",
                published=h.get("created_at"),
            )
            for h in r.json().get("hits", [])
            if h.get("title")
        ]
    return items


POLLERS: dict[str, Poller] = {
    "topic": poll_topic,
    "rss": poll_rss,
    "jobtech": poll_jobtech,
    "devpost": poll_devpost,
    "ats": poll_ats,
    "hn_hiring": poll_hn_hiring,
}
