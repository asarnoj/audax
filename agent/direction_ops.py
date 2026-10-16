"""Changes to what the agent looks for: missions (config/research.yaml) and senses (sources.yaml,
watchlist.yaml). Owned by Director (docs/MODEL.md); the chat uses the same operations when
the owner asks directly. All config, applied live by the reload watcher, reversible via git.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess

import httpx
import yaml

from agent.netguard import public_client
from agent.config import INSTANCE, instance_config
from agent.sources.streams import USER_AGENT

log = logging.getLogger(__name__)

RESEARCH_FILE = instance_config("research.yaml")
WATCHLIST_FILE = instance_config("watchlist.yaml")
SOURCES_FILE = instance_config("sources.yaml")


def _write_yaml_keep_header(path, data) -> None:
    header = "".join(line + "\n" for line in path.read_text().splitlines() if line.startswith("#"))
    path.write_text(header + "\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100))


def commit_config(message: str) -> None:
    """The agent's own config edits are committed at once in the instance repo, so every change is
    versioned, reversible and included in the backup, and pushed to the instance's `github` remote."""
    files = [str(p.relative_to(INSTANCE)) for p in (RESEARCH_FILE, WATCHLIST_FILE, SOURCES_FILE) if p.is_relative_to(INSTANCE)]
    if not files or not (INSTANCE / ".git").exists():
        return  # tests point the files elsewhere
    git = ["git", "-C", str(INSTANCE), "-c", "user.name=agent", "-c", "user.email=agent@localhost"]
    try:
        if subprocess.run([*git, "diff", "--quiet", "--", *files]).returncode != 0:
            subprocess.run([*git, "commit", "-q", "-m", message, "--", *files], check=True, capture_output=True)
            # Off-site copy: push to the private instance repo if one is configured (in the background).
            if subprocess.run([*git, "remote", "get-url", "github"], capture_output=True).returncode == 0:
                subprocess.Popen([*git, "push", "-q", "github", "main"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # never let bookkeeping break the agent
        log.warning("config commit failed: %s", e)


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().translate(str.maketrans("åäöé", "aaoe"))).strip("-")
    return s[:40] or "x"


def apply_missions(ops: list[dict]) -> list[str]:
    """ops: {op: add|update|disable, id, goal?, scope?, interval_days?, reason}"""
    data = yaml.safe_load(RESEARCH_FILE.read_text()) or []
    by_id = {r["id"]: r for r in data}
    changes = []
    for o in ops:
        mid = slug(o.get("id") or o.get("goal", ""))
        r = by_id.get(mid)
        if o["op"] == "add" and r is None and o.get("goal"):
            r = {"id": mid, "goal": o["goal"], "scope": o.get("scope") or "", "interval_days": 3.0, "enabled": True}
            data.append(r)
            by_id[mid] = r
        elif r is None:
            continue
        if o["op"] == "disable":
            r["enabled"] = False
        else:
            r["enabled"] = True
            for key in ("goal", "scope"):
                if o.get(key):
                    r[key] = o[key]
            if o.get("interval_days"):
                r["interval_days"] = max(0.5, float(o["interval_days"]))  # sanity bound: at most twice a day
        changes.append(f"uppdrag {mid} ({o['op']}): {o.get('reason', '')}")
    if changes:
        _write_yaml_keep_header(RESEARCH_FILE, data)
        commit_config("agent: uppdrag\n\n" + "\n".join(changes))
    return changes


async def add_company(name: str, careers_url: str) -> str:
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
    add_page(f"careers-{name}", careers_url, CAREERS_NOTE.format(name=name), "opportunity")
    return f"{name} bevakas nu via karriärsidan (inget känt rekryteringssystem)."


CAREERS_NOTE = "Karriärsida för {name}. Värdefullt: praktik, sommarjobb, student-, trainee- och juniorroller inom mjukvara."


def watch_unwatched_companies() -> list[str]:
    """Companies on the watchlist without a known ATS were never polled (found 12 Oct). Watch their careers pages."""
    data = yaml.safe_load(WATCHLIST_FILE.read_text()) or {}
    streams = (yaml.safe_load(SOURCES_FILE.read_text()) or {}).get("streams") or []
    urls = {s.get("url") for s in streams}
    added = []
    for c in data.get("companies") or []:
        if c.get("ats") or not c.get("careers_url") or c["careers_url"] in urls:
            continue
        add_page(f"careers-{c['name']}", c["careers_url"], CAREERS_NOTE.format(name=c["name"]), "opportunity")
        added.append(c["name"])
    return added


def add_page(page_id: str, url: str, note: str = "", kind: str = "event", type_: str = "page") -> str:
    """Appends a page watcher (or rss feed) to sources.yaml; streams is the last key, live reload picks it up."""
    sid = slug(page_id)
    data = yaml.safe_load(SOURCES_FILE.read_text()) or {}
    if any(s.get("id") == sid or s.get("url") == url for s in data.get("streams") or []):
        return f"{url} bevakas redan."
    block = (
        f"\n  - id: {sid}\n    type: {type_}\n    url: {url}\n    kind: {kind}\n"
        f"    baseline: false\n    poll_minutes: 360\n    note: {json.dumps(note, ensure_ascii=False)}\n"
    )
    with SOURCES_FILE.open("a") as f:
        f.write(block)
    commit_config(f"agent: bevaka {url}")
    what = "flödet" if type_ == "rss" else "sidan"
    return f"Bevakar {what} {url} från och med nu, kollar ungefär var 6:e timme. Bara det som är nytt räknas."


def add_topic(name: str, query: str, lang: str = "en", note: str = "") -> str:
    """Follow news and Hacker News about a topic. The first poll only records what exists (baseline),
    so only new articles reach triage."""
    sid = "topic-" + slug(name)
    data = yaml.safe_load(SOURCES_FILE.read_text()) or {}
    if any(s.get("id") == sid for s in data.get("streams") or []):
        return f"Ämnet {name} följs redan."
    block = (
        f"\n  - id: {sid}\n    type: topic\n    kind: news\n    poll_minutes: 120\n"
        f"    params: {{query: {json.dumps(query, ensure_ascii=False)}, lang: {lang}}}\n"
        f"    note: {json.dumps(note or f'Nyheter om {name}. Bara det som är verkligt nytt eller viktigt.', ensure_ascii=False)}\n"
    )
    with SOURCES_FILE.open("a") as f:
        f.write(block)
    commit_config(f"agent: följ ämnet {name}")
    return (f"Följer nyheter om {name} från och med nu: nyhetssajter (via nyhetssök) och Hacker News, kollar varannan timme. "
            f"Det som redan är publicerat räknas inte, bara nytt. Det som är värt något skickas, resten hamnar på hyllan.")


def _stream_block(text: str, stream_id: str) -> tuple[int, int] | None:
    """(start, end) of the '  - id: <stream_id>' block in sources.yaml, so edits keep comments and layout."""
    m = re.search(rf"(?m)^  - id: {re.escape(stream_id)}\s*$", text)
    if not m:
        return None
    nxt = re.search(r"(?m)^  (?:- |#)|^\S", text[m.end():])
    return m.start(), (m.end() + nxt.start()) if nxt else len(text)


def _set_field(stream_id: str, key: str, value: str) -> bool:
    text = SOURCES_FILE.read_text()
    span = _stream_block(text, stream_id)
    if span is None:
        return False
    a, b = span
    block = text[a:b]
    line = f"    {key}: {value}\n"
    if re.search(rf"(?m)^    {key}:.*\n", block):
        block = re.sub(rf"(?m)^    {key}:.*\n", line, block, count=1)
    else:
        first_line_end = block.index("\n") + 1
        block = block[:first_line_end] + line + block[first_line_end:]
    SOURCES_FILE.write_text(text[:a] + block + text[b:])
    return True


def tune_sense(stream_id: str, poll_minutes: float, reason: str = "") -> str:
    """How often a sense is checked. Sanity bounds only (not policy): 15 min to a week."""
    minutes = int(min(max(float(poll_minutes), 15), 7 * 24 * 60))
    if not _set_field(stream_id, "poll_minutes", str(minutes)):
        return f"Hittar inget sinne {stream_id}."
    commit_config(f"agent: takt {stream_id} -> {minutes} min\n\n{reason}")
    return f"{stream_id}: var {minutes} min"


def disable_sense(stream_id: str, reason: str = "") -> str:
    if not _set_field(stream_id, "enabled", "false"):
        return f"Hittar inget sinne {stream_id}."
    commit_config(f"agent: stäng av {stream_id}\n\n{reason}")
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
