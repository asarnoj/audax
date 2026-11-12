"""What the prompts hold and what of it is used (instance/granskning/2026-11-12-omgang-16-kunskapen.md, "Mätning").

Read from what every run already keeps (claude_runs: the prompt and, in order, the tool calls and their results):

- the prompt's size;
- the ids it showed (#S5, #U31, D12 …) and how many of them the run used: named in a tool call, in its own words or
  in its answer, not only in what a tool brought back;
- how often it fetched (read, find, thread, the search);
- the points: raised, answered, resolved by themselves, open; and how often "tried before" was shown.

Run on the Pi: ./deploy/audax prompts [days] [friend]. Measured before stage 1 moved anything, and after each stage.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from agent.db import Database
from agent.forms import LETTERS

ID = re.compile(r"(?<![\w#])#?([" + "".join(LETTERS) + r"PFB]\d+|D\d+)\b")
FETCH = ("office__read", "office__find", "office__thread", "office__forms", "lookup", "search", "look_for")


def ids_in(text: str) -> set[str]:
    return {m.group(1).upper() for m in ID.finditer(text or "") if not m.group(1)[1:].startswith("0")}


async def runs(db: Database, days: int) -> dict[str, dict]:
    per: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    rows = await db.fetchall("SELECT kind, prompt, transcript FROM claude_runs WHERE prompt IS NOT NULL AND created_at >= datetime('now', ?)",
                             (f"-{days} days",))
    for r in rows:
        m = per[r["kind"]]
        m["runs"] += 1
        m["chars"] += len(r["prompt"])
        shown = ids_in(r["prompt"])
        said, fetches = set(), 0
        for part in json.loads(r["transcript"] or "[]"):
            if "tool" in part:
                said |= ids_in(part.get("input") or "")
                fetches += part["tool"].split("__")[-1] in {f.split("__")[-1] for f in FETCH}
            elif "text" in part:
                said |= ids_in(part["text"])
        m["shown"] += len(shown)
        m["used"] += len(shown & said)
        m["fetches"] += fetches
    return per


async def report(db: Database, days: int = 14) -> str:
    from agent import docket

    lines = [f"Prompterna, senaste {days} dagarna (medel per körning)",
             f"{'körning':<22}{'antal':>6}{'tecken':>9}{'id visade':>11}{'använda':>9}{'andel':>7}{'hämtningar':>12}"]
    for kind, m in sorted((await runs(db, days)).items(), key=lambda kv: -kv[1]["chars"] / kv[1]["runs"]):
        n = m["runs"]
        share = f"{m['used'] / m['shown']:.0%}" if m["shown"] else "–"
        lines.append(f"{kind:<22}{int(n):>6}{m['chars'] / n:>9.0f}{m['shown'] / n:>11.1f}{m['used'] / n:>9.1f}{share:>7}{m['fetches'] / n:>12.1f}")
    st = await docket.stats(db, days)
    lines.append("")
    lines.append("Punkter att avgöra (öppnade senaste dagarna): " + ("; ".join(
        f"{role}: " + ", ".join(f"{k} {v}" for k, v in sorted(s.items())) for role, s in sorted(st.items())) or "inga"))
    r = await db.fetchone("SELECT count(*) AS n FROM office_events WHERE kind = 'tried_before' AND created_at >= datetime('now', ?)",
                          (f"-{days} days",))
    links = await db.fetchone("SELECT count(*) AS n FROM item_links WHERE relation = 'mentions'")
    lines.append(f"\"Tidigare prövat\" visat: {r['n'] if r else 0} gånger · länkar koden gjort ur id i texten: {links['n'] if links else 0}")
    return "\n".join(lines)


async def _main(argv: list[str]) -> None:
    days = int(argv[0]) if argv and argv[0].isdigit() else 14
    db = Database(Path(argv[1]) if len(argv) > 1 else Path("data/agent.db"))
    await db.open()
    try:
        print(await report(db, days))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1:]))
