"""Watchlist tooling: `uv run python -m agent.sources.watchlist detect` finds the applicant tracking
system for companies that only have a careers_url, and writes it back to config/watchlist.yaml."""

from __future__ import annotations

import asyncio
import sys

import httpx
import yaml

from agent.sources.connectors import WATCHLIST, Company, detect_ats, fetch_company_jobs
from agent.sources.streams import USER_AGENT


async def detect() -> None:
    text = WATCHLIST.read_text()
    header = "".join(line + "\n" for line in text.splitlines() if line.startswith("#"))
    data = yaml.safe_load(text) or {}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        for c in data.get("companies", []):
            if c.get("ats") or not c.get("careers_url"):
                continue
            try:
                found = await detect_ats(c["careers_url"], client)
                if found:
                    jobs = await fetch_company_jobs(Company(c["name"], *found), client)
                    c["ats"], c["slug"] = found
                    print(f"✅ {c['name']}: {found[0]} / {found[1]} ({len(jobs)} annonser)")
                else:
                    print(f"–  {c['name']}: hittade inget känt system")
            except Exception as e:
                print(f"–  {c['name']}: {type(e).__name__}: {e}")
    WATCHLIST.write_text(header + "\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120))


if __name__ == "__main__":
    if sys.argv[1:] == ["detect"]:
        asyncio.run(detect())
    else:
        print(__doc__)
