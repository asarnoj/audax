"""The web layer: reading pages reliably (docs/SENSES.md).

Claude's built-in WebFetch gets blocked by bot protection on many sites (e.g. a 403 from Devpost),
and when it failed the agent simply gave up. web_read tries several ways in order and remembers per
domain which one works:

1. Known source -> its API (Devpost, GitHub, Lever, Greenhouse, Ashby): structured and complete
2. A plain fetch with an honest user agent (bot protection tends to block clients pretending to be browsers)
3. A reader service (r.jina.ai) that renders the page, as a last resort. It learns which public URL is
   read, nothing about the owner.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx

from agent.db import Database
from agent.netguard import PrivateAddress, check_public, public_client
from agent.sources.pages import page_text
from agent.sources.streams import USER_AGENT

log = logging.getLogger(__name__)

MAX_CHARS = 15000
BLOCK_MARKERS = (
    "just a moment", "cf-chl", "enable javascript and cookies", "attention required", "access denied", "captcha",
    "unsupported browser",  # e.g. Devpost serves a shell page to unknown clients
)


def _blocked(status: int, text: str) -> bool:
    if status in (401, 403, 406, 429, 503):
        return True
    head = text[:20000].lower()
    return len(text) < 400 or any(m in head for m in BLOCK_MARKERS)


class WebReader:
    def __init__(self, db: Database | None = None, github=None, transport: httpx.AsyncBaseTransport | None = None, reader_service: bool = True):
        self.db = db
        self.github = github  # GitHubReader, for github.com URLs
        self._transport = transport
        self.reader_service = reader_service

    def _client(self) -> httpx.AsyncClient:
        return public_client(self._transport, timeout=25, follow_redirects=True, headers={"User-Agent": USER_AGENT})

    async def read(self, url: str) -> tuple[str, str]:
        """Returns (text, method). Text starts with an error line if nothing worked."""
        if not re.match(r"^https?://", url):
            url = "https://" + url
        try:
            await check_public(url, resolve=self._transport is None)
        except PrivateAddress as e:
            log.warning("web_read refused: %s", e)
            return f"[internal] Refused: Audax only reads public web pages ({e}).", "none"
        domain = urlparse(url).netloc.lower().removeprefix("www.")
        order = ["api", "direct", "reader"]
        if self.db and (best := await self._best_method(domain)):
            order.remove(best)
            order.insert(0, best)
        errors = []
        for method in order:
            if method == "reader" and not self.reader_service:
                continue
            try:
                text = await getattr(self, f"_{method}")(url, domain)
            except Exception as e:
                errors.append(f"{method}: {type(e).__name__}")
                await self._record(domain, method, False, str(e)[:100])
                continue
            if text:
                await self._record(domain, method, True)
                return (text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + f"\n… (avkortad, {len(text)} tecken)"), method
            if method != "api":
                errors.append(f"{method}: blockerad/tom")
                await self._record(domain, method, False, "blocked or empty")
        return f"Kunde inte läsa {url} ({'; '.join(errors)}).", "none"

    # --- methods ------------------------------------------------------------

    async def _api(self, url: str, domain: str) -> str:
        """Known sources: answer from their API instead of scraping. Empty string = not applicable."""
        path = urlparse(url).path.strip("/")
        async with self._client() as c:
            if domain.endswith("devpost.com") and domain != "devpost.com":
                slug = domain.split(".")[0]
                r = await c.get("https://devpost.com/api/hackathons", params={"search": slug.replace("-", " ")})
                r.raise_for_status()
                h = next((x for x in r.json().get("hackathons", []) if domain in x.get("url", "")), None)
                if h is None:
                    return ""
                api = (
                    f"[Devpost API] {h['title']}\nStatus: {h.get('open_state')} · {h.get('submission_period_dates')} · "
                    f"{(h.get('displayed_location') or {}).get('location', '')}\nAnmälda: {h.get('registrations_count')} · "
                    f"Pris: {re.sub('<[^>]+>', '', h.get('prize_amount') or '')} · Teman: {', '.join(t['name'] for t in h.get('themes', []))}\n"
                    f"Arrangör: {h.get('organization_name') or '?'} · {h['url']}"
                )
                # Description, eligibility, fee and team size live on the pages, not in the API.
                base = h["url"].rstrip("/")
                page = await self._direct(base, domain) or await self._reader(base, domain)
                rules = await self._direct(base + "/rules", domain) or await self._reader(base + "/rules", domain)
                return api + (f"\n\n[Sidan]\n{page[:6000]}" if page else "") + (f"\n\n[Regler]\n{rules[:6000]}" if rules else "")
            if domain == "github.com" and self.github and path.count("/") >= 1:
                owner, repo, *rest = path.split("/")
                if rest[:1] in (["blob"], ["tree"]) and len(rest) >= 3:
                    return "[GitHub]\n" + await self.github.read(f"{owner}/{repo}", "/".join(rest[2:]))
                return "[GitHub]\n" + await self.github.repo(f"{owner}/{repo}")
            if domain == "jobs.lever.co" and path.count("/") == 1:
                company, jid = path.split("/")
                r = await c.get(f"https://api.lever.co/v0/postings/{company}/{jid}")
                if r.status_code == 200:
                    j = r.json()
                    return f"[Lever] {j['text']} · {(j.get('categories') or {}).get('location', '')}\n{j.get('descriptionPlain', '')}\n" + "\n".join(
                        f"{x['text']}: {re.sub('<[^>]+>', ' ', x.get('content', ''))}" for x in j.get("lists", [])
                    )
        return ""

    async def _direct(self, url: str, domain: str) -> str:
        async with self._client() as c:
            r = await c.get(url)
        if "html" not in r.headers.get("content-type", "html"):
            return r.text if r.status_code == 200 else ""
        if _blocked(r.status_code, r.text):
            return ""
        return "\n".join(page_text(r.text, str(r.url)))

    async def _reader(self, url: str, domain: str) -> str:
        async with self._client() as c:
            r = await c.get(f"https://r.jina.ai/{url}", headers={"Accept": "text/plain"})
        return r.text if r.status_code == 200 and len(r.text) > 200 else ""

    # --- per-domain memory --------------------------------------------------

    async def _record(self, domain: str, method: str, ok: bool, error: str | None = None) -> None:
        if not self.db:
            return
        await self.db.execute(
            "INSERT INTO web_domains (domain, method, ok, fail, last_error) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(domain, method) DO UPDATE SET ok = ok + excluded.ok, fail = fail + excluded.fail, "
            "last_error = COALESCE(excluded.last_error, last_error), updated_at = datetime('now')",
            (domain, method, int(ok), int(not ok), error),
        )

    async def _best_method(self, domain: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT method FROM web_domains WHERE domain = ? AND ok > 0 ORDER BY ok * 1.0 / (ok + fail) DESC, ok DESC LIMIT 1", (domain,)
        )
        return row["method"] if row else None

    async def stats(self, limit: int = 12) -> str:
        """Which domains are hard to read, for the Director."""
        if not self.db:
            return ""
        rows = await self.db.fetchall(
            "SELECT domain, group_concat(method || ' ' || ok || '/' || (ok + fail), ', ') AS m, sum(fail) AS f FROM web_domains "
            "WHERE updated_at >= datetime('now', '-30 days') GROUP BY domain ORDER BY f DESC LIMIT ?",
            (limit,),
        )
        return "\n".join(f"- {r['domain']}: {r['m']}" for r in rows) or "Ingen läsning loggad än."
