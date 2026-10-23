"""Code before Claude (instance/granskning/2026-10-20-omgang-3-flodet.md, "Kod före Claude"): what can be decided
without judgement is decided here, before triage asks Claude, so Claude only sees what needs it.

Built in, always on:
- a duplicate: the same address (normalised) or the same title as a finding of the last 30 days, from any source
  (on 12 Oct the same news came twice via two sources; triage judged one event four times);
- already past: an event whose date, or anything whose deadline, has passed.

Made by the roles, as rules (#G, agent/forms.py): `applies_to` is a source id, a kind of finding, or "all";
`condition` is JSON, {"any": [words], "all": [words], "none": [words]}, matched case-insensitively on the title and
the text; the action `drop` throws it away with the rule's reason. Every hit is counted on the rule, so one that
never hits, or stops everything, shows up in its owner's cleaning pass.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from agent.db import Database
from agent.items import Items

log = logging.getLogger(__name__)


def _norm_url(url: str | None) -> str:
    from agent.research import normalize_url

    return normalize_url(url) if url else ""


def _norm_title(title: str | None) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in (title or "").casefold()).split())


def _host(url: str | None) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).netloc.lower().removeprefix("www.") if url else "")


def _same_story(title: str, host: str, seen: dict, batch: dict) -> str | None:
    """The same title is the same news when it comes from another site, or has no address. On the same site with
    another address it is something else with the same name: job postings for different cities share a title
    (23 Oct: Google's London and EMEA internships)."""
    if title in seen:
        fid, seen_host = seen[title]
        if not host or not seen_host or host != seen_host:
            return f"Dubblett: samma rubrik som #F{fid}."
    if title in batch and (not host or not batch[title] or batch[title] != host):
        return "Dubblett i samma omgång."
    return None


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def matches(condition: dict, text: str) -> bool:
    hay = text.casefold()
    any_ = [w.casefold() for w in condition.get("any") or []]
    all_ = [w.casefold() for w in condition.get("all") or []]
    none = [w.casefold() for w in condition.get("none") or []]
    if any_ and not any(w in hay for w in any_):
        return False
    if all_ and not all(w in hay for w in all_):
        return False
    if none and any(w in hay for w in none):
        return False
    return bool(any_ or all_ or none)


async def screen(db: Database, rows: list, tz: str = "Europe/Stockholm") -> dict[int, str]:
    """{event id: why it is thrown away} for the events code can decide on; the rest go to Claude."""
    today = datetime.now(ZoneInfo(tz)).date()
    recent = await db.fetchall("SELECT id, url, title FROM candidates WHERE created_at >= datetime('now', '-30 days')")
    seen_urls = {_norm_url(r["url"]): r["id"] for r in recent if r["url"]}
    seen_titles = {_norm_title(r["title"]): (r["id"], _host(r["url"])) for r in recent if len(_norm_title(r["title"])) > 12}
    rules = []
    for r in await Items(db).find("rule", status="active"):
        try:
            rules.append((r, json.loads(r.get("condition") or "{}")))
        except ValueError:
            log.warning("rule #G%s has a condition that isn't JSON", r["id"])
    out: dict[int, str] = {}
    batch_urls, batch_titles = set(), {}
    for row in rows:
        p = json.loads(row["payload"] or "{}")
        url, title, host = _norm_url(p.get("url")), _norm_title(p.get("title")), _host(p.get("url"))
        if url and (url in seen_urls or url in batch_urls):
            out[row["id"]] = f"Dubblett: samma adress som #F{seen_urls[url]}." if url in seen_urls else "Dubblett i samma omgång."
        elif title and len(title) > 12 and (same := _same_story(title, host, seen_titles, batch_titles)):
            out[row["id"]] = same
        elif (d := _day(p.get("deadline"))) and d < today:
            out[row["id"]] = f"Sista dagen har passerat ({d.isoformat()})."
        elif row["kind"] == "event" and (d := _day(p.get("date"))) and d < today:
            out[row["id"]] = f"Eventet har redan varit ({d.isoformat()})."
        else:
            source = (row["source"] or "").removeprefix("stream:")
            text = f"{p.get('title', '')}\n{p.get('summary', '')}"
            for rule, condition in rules:
                target = rule.get("applies_to", "all")
                if target not in ("all", source, row["source"], row["kind"]):
                    continue
                if rule.get("action") == "drop" and matches(condition, text):
                    out[row["id"]] = f"Regel #G{rule['id']}: {rule.get('reason', '')}"
                    await db.execute("UPDATE items SET data = json_set(data, '$.hits', coalesce(json_extract(data, '$.hits'), 0) + 1) WHERE id = ?",
                                     (rule["id"],))
                    break
        batch_urls.add(url)
        batch_titles.setdefault(title, host)
    return out
