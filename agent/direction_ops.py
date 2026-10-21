"""Changes to what the agent looks for: search missions and watched sources, both routines in the office's core
(agent/routines.py), and the company watchlist (watchlist.yaml). Researcher's, through its planning; the chat uses
the same operations when the owner asks directly. Every change is logged in the routine's history.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess

import yaml

from agent import routines
from agent.config import INSTANCE, instance_config
from agent.db import Database
from agent.netguard import public_client
from agent.sources.streams import USER_AGENT

log = logging.getLogger(__name__)

WATCHLIST_FILE = instance_config("watchlist.yaml")


def commit_config(message: str) -> None:
    """The watchlist's edits are committed at once in the instance repo, so every change is versioned, reversible
    and included in the backup, and pushed to the instance's `github` remote."""
    files = [str(p.relative_to(INSTANCE)) for p in (WATCHLIST_FILE,) if p.is_relative_to(INSTANCE)]
    if not files or not (INSTANCE / ".git").exists():
        return  # tests point the files elsewhere
    git = ["git", "-C", str(INSTANCE), "-c", "user.name=agent", "-c", "user.email=agent@localhost"]
    try:
        if subprocess.run([*git, "diff", "--quiet", "--", *files]).returncode != 0:
            subprocess.run([*git, "commit", "-q", "-m", message, "--", *files], check=True, capture_output=True)
            if subprocess.run([*git, "remote", "get-url", "github"], capture_output=True).returncode == 0:
                subprocess.Popen([*git, "push", "-q", "github", "main"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # never let bookkeeping break the agent
        log.warning("config commit failed: %s", e)


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().translate(str.maketrans("åäöé", "aaoe"))).strip("-")
    return s[:40] or "x"


async def apply_missions(db: Database, ops: list[dict], by: str = "researcher") -> list[str]:
    """ops: {op: add|update|disable, id, goal?, scope?, interval_days?, reason}"""
    changes = []
    for o in ops:
        mid = slug(o.get("id") or o.get("goal", ""))
        existing = await routines.find(db, "mission", mid)
        if existing is None and not (o["op"] == "add" and o.get("goal")):
            continue
        m = dict(existing["_config"]["mission"]) if existing else {"id": mid, "goal": o["goal"], "scope": o.get("scope") or "", "interval_days": 3.0}
        if o["op"] == "disable":
            m["enabled"] = False
        else:
            m["enabled"] = True
            for key in ("goal", "scope"):
                if o.get(key):
                    m[key] = o[key]
            if o.get("interval_days"):
                m["interval_days"] = max(0.5, float(o["interval_days"]))  # sanity bound: at most twice a day
        await routines.put_mission(db, m, by, note=o.get("reason", ""))
        changes.append(f"uppdrag {mid} ({o['op']}): {o.get('reason', '')}")
    return changes


async def add_company(db: Database, name: str, careers_url: str) -> str:
    data = yaml.safe_load(WATCHLIST_FILE.read_text()) or {}
    if any(c.get("name", "").lower() == name.lower() for c in data.get("companies", [])):
        return f"{name} bevakas redan."
    entry = {"name": name, "careers_url": careers_url}
    reachable = False
    try:
        from agent.sources import connectors

        async with public_client(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            if found := await connectors.detect_ats(careers_url, client):
                entry["ats"], entry["slug"] = found
            reachable = (await client.get(careers_url)).status_code < 400
    except Exception as e:
        log.warning("detect_ats %s: %s", careers_url, e)
    if "ats" not in entry and not reachable:
        return f"{name}: karriärsidan {careers_url} svarar inte. Hitta rätt adress (t.ex. med en sökning) och lägg till igen."
    text = WATCHLIST_FILE.read_text().rstrip("\n")
    WATCHLIST_FILE.write_text(text + "\n  - " + json.dumps(entry, ensure_ascii=False) + "\n")
    commit_config(f"agent: bevaka {name}")
    if "ats" in entry:
        return f"{name} bevakas nu ({entry['ats']})."
    # No known applicant tracking system: watch the careers page itself, so the company isn't silently unwatched.
    await add_page(db, f"careers-{name}", careers_url, CAREERS_NOTE.format(name=name), "opportunity")
    return f"{name} bevakas nu via karriärsidan (inget känt rekryteringssystem)."


CAREERS_NOTE = "Karriärsida för {name}. Värdefullt: praktik, sommarjobb, student-, trainee- och juniorroller inom mjukvara."


async def add_page(db: Database, page_id: str, url: str, note: str = "", kind: str = "event", type_: str = "page",
                   urgent: bool = False, by: str = "researcher") -> str:
    """A page watcher (or rss feed) as a routine; it starts running at once."""
    sid = slug(page_id)
    if await routines.find(db, "stream", sid) or any(s.get("url") == url for s in routines.streams() or []):
        return f"{url} bevakas redan."
    stream = {"id": sid, "type": type_, "url": url, "kind": kind, "baseline": False, "poll_minutes": 360, "note": note,
              **({"urgent": True} if urgent else {})}
    await routines.put_stream(db, stream, by, note=note or "ny bevakning")
    what = "flödet" if type_ == "rss" else "sidan"
    return f"Bevakar {what} {url} från och med nu, kollar ungefär var 6:e timme. Bara det som är nytt räknas."


async def add_topic(db: Database, name: str, query: str, lang: str = "en", note: str = "", urgent: bool = False,
                    by: str = "researcher") -> str:
    """Follow news and Hacker News about a topic. The first poll only records what exists (baseline), so only new
    articles reach triage."""
    sid = "topic-" + slug(name)
    if await routines.find(db, "stream", sid):
        return f"Ämnet {name} följs redan."
    stream = {"id": sid, "type": "topic", "kind": "news", "poll_minutes": 120, "params": {"query": query, "lang": lang},
              "note": note or f"Nyheter om {name}. Bara det som är verkligt nytt eller viktigt.", **({"urgent": True} if urgent else {})}
    await routines.put_stream(db, stream, by, note=stream["note"])
    return (f"Följer nyheter om {name} från och med nu: nyhetssajter (via nyhetssök) och Hacker News, kollar varannan timme. "
            f"Det som redan är publicerat räknas inte, bara nytt. Det som är värt något skickas, resten hamnar på hyllan.")


async def tune_sense(db: Database, stream_id: str, poll_minutes: float, reason: str = "", by: str = "researcher") -> str:
    """How often a source is checked. Sanity bounds only (not policy): 15 min to a week."""
    minutes = int(min(max(float(poll_minutes), 15), 7 * 24 * 60))
    if not await routines.set_stream(db, stream_id, by, note=reason or f"takt {minutes} min", poll_minutes=minutes):
        return f"Hittar inget sinne {stream_id}."
    return f"{stream_id}: var {minutes} min"


async def disable_sense(db: Database, stream_id: str, reason: str = "", by: str = "researcher") -> str:
    if not await routines.pause(db, "stream", stream_id, by, reason or "avstängd"):
        return f"Hittar inget sinne {stream_id}."
    return f"{stream_id}: avstängt"


async def verify_source(type_: str, url_or_query: str, lang: str = "en") -> str:
    """Fetches a new source once so the owner hears whether it works, not 'I haven't tested it'."""
    from agent.sources.connectors import poll_topic
    from agent.sources.pages import page_text
    from agent.sources.streams import StreamConfig, parse_feed

    try:
        async with public_client(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            if type_ == "topic":
                items = await poll_topic(StreamConfig(id="t", type="topic", url="", params={"query": url_or_query, "lang": lang}), client)
                return f"Kontrollerat: just nu {len(items)} artiklar om ämnet" + (f", senast \"{items[0].title[:70]}\"." if items else ".")
            r = await client.get(url_or_query)
            if r.status_code != 200:
                return f"Varning: källan svarade {r.status_code}. Den bevakas, men kan behöva bytas."
            if type_ == "rss":
                items = parse_feed(r.content)
                return f"Kontrollerat: flödet fungerar, {len(items)} inlägg" + (f", senast \"{items[0].title[:70]}\"." if items else ", men tomt just nu.")
            lines = page_text(r.text, str(r.url))
            return f"Kontrollerat: sidan går att läsa ({len(lines)} rader text). Nytt som dyker upp där fångas."
    except Exception as e:
        return f"Varning: källan gick inte att läsa just nu ({type(e).__name__})."
