"""Changes to what the agent looks for: missions (config/research.yaml) and senses (sources.yaml,
watchlist.yaml). Owned by Director (docs/ARCHITECTURE.md); the chat uses the same operations when
the owner asks directly. All config, applied live by the reload watcher, reversible via git.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess

import httpx
import yaml

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
    versioned, reversible and included in the backup."""
    files = [str(p.relative_to(INSTANCE)) for p in (RESEARCH_FILE, WATCHLIST_FILE, SOURCES_FILE) if p.is_relative_to(INSTANCE)]
    if not files or not (INSTANCE / ".git").exists():
        return  # tests point the files elsewhere
    git = ["git", "-C", str(INSTANCE), "-c", "user.name=agent", "-c", "user.email=agent@localhost"]
    try:
        if subprocess.run([*git, "diff", "--quiet", "--", *files]).returncode != 0:
            subprocess.run([*git, "commit", "-q", "-m", message, "--", *files], check=True, capture_output=True)
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
    try:
        from agent.sources import connectors

        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            if found := await connectors.detect_ats(careers_url, client):
                entry["ats"], entry["slug"] = found
    except Exception as e:
        log.warning("detect_ats %s: %s", careers_url, e)
    text = WATCHLIST_FILE.read_text().rstrip("\n")
    WATCHLIST_FILE.write_text(text + "\n  - " + json.dumps(entry, ensure_ascii=False) + "\n")
    commit_config(f"agent: bevaka {name}")
    if "ats" in entry:
        return f"{name} bevakas nu ({entry['ats']})."
    return f"{name} ligger på bevakningslistan, men rekryteringssystemet gick inte att hitta automatiskt."


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
    return f"Bevakar {url} från och med nu."
