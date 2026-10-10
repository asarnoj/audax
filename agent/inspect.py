"""Inspection: what the agent has done, decided and failed at. Read-only.

    uv run python -m agent.inspect [today|day YYYY-MM-DD|runs [kind] [n]|decisions [n]|research|senses|errors]

From the development machine: ./deploy/audax <same arguments> (runs it on the Pi over SSH).
The chat uses `summary()` to answer "what have you done today?".
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from agent.config import load_settings
from agent.db import Database

WEEKDAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]


class Inspector:
    def __init__(self, db: Database, tz: str = "Europe/Stockholm"):
        self.db = db
        self.tz = ZoneInfo(tz)

    def _t(self, sqlite_utc: str) -> str:
        return datetime.fromisoformat(sqlite_utc + "+00:00").astimezone(self.tz).strftime("%H:%M")

    def _bounds(self, day: date) -> tuple[str, str]:
        start = datetime.combine(day, datetime.min.time(), tzinfo=self.tz).astimezone(ZoneInfo("UTC"))
        fmt = "%Y-%m-%d %H:%M:%S"
        return start.strftime(fmt), (start + timedelta(days=1)).strftime(fmt)

    # --- health -------------------------------------------------------------

    async def health(self) -> list[str]:
        """Problems worth knowing about. Empty list = healthy."""
        problems = []
        recent = await self.db.fetchall(
            "SELECT kind, is_error, error FROM claude_runs WHERE created_at >= datetime('now', '-2 hours') ORDER BY id DESC LIMIT 6"
        )
        if len(recent) >= 3 and all(r["is_error"] for r in recent[:3]):
            problems.append(f"Claude misslyckas upprepat ({recent[0]['kind']}: {(recent[0]['error'] or '')[:120]})")
        rl = await self.db.fetchone("SELECT rate_limit FROM claude_runs WHERE rate_limit IS NOT NULL ORDER BY id DESC LIMIT 1")
        if rl and json.loads(rl["rate_limit"]).get("status") == "rejected":
            problems.append("Claude-kvoten är slut, bakgrundsjobben väntar tills den återställs")
        last_any = await self.db.fetchone("SELECT max(created_at) AS t FROM claude_runs WHERE is_error = 0")
        if last_any and last_any["t"] and datetime.fromisoformat(last_any["t"] + "+00:00") < datetime.now(ZoneInfo("UTC")) - timedelta(hours=8):
            problems.append(f"Ingen lyckad Claude-körning sedan {self._t(last_any['t'])} (över 8 timmar)")
        return problems

    # --- views --------------------------------------------------------------

    async def day(self, day: date | None = None) -> str:
        day = day or datetime.now(self.tz).date()
        a, b = self._bounds(day)
        problems = await self.health()
        out = [f"=== {WEEKDAYS[day.weekday()]} {day} ===", "Hälsa: " + ("OK" if not problems else "; ".join(problems)), ""]

        msgs = await self.db.fetchall(
            "SELECT created_at, direction, origin, text FROM messages WHERE created_at >= ? AND created_at < ? ORDER BY id", (a, b)
        )
        out.append(f"Meddelanden ({sum(m['direction'] == 'in' for m in msgs)} in, {sum(m['direction'] == 'out' for m in msgs)} ut):")
        for m in msgs:
            who = "du " if m["direction"] == "in" else {"proactive": "BRUNO→", "command": "kmd  "}.get(m["origin"], "svar ")
            out.append(f"  {self._t(m['created_at'])} {who} {m['text'][:110].replace(chr(10), ' / ')}")

        runs = await self.db.fetchall(
            "SELECT research_id, findings, returned, skipped, error, created_at FROM research_runs WHERE created_at >= ? AND created_at < ?", (a, b)
        )
        if runs:
            out += ["", "Undersökningar:"]
            for r in runs:
                skipped = json.loads(r["skipped"] or "[]")
                out.append(
                    f"  {self._t(r['created_at'])} {r['research_id']}: {r['returned'] if r['returned'] is not None else '?'} fynd, {r['findings']} nya, "
                    f"{len(skipped)} bortvalda" + (f", FEL: {r['error']}" if r["error"] else "")
                )
                for s in skipped[:4]:
                    out.append(f"      bortvald: {s['title'][:60]} ({s['reason'][:60]})")

        dec = await self.db.fetchall(
            "SELECT d.tier, count(*) AS n FROM decisions d WHERE d.created_at >= ? AND d.created_at < ? GROUP BY d.tier ORDER BY n DESC", (a, b)
        )
        out += ["", "Dispatcherns beslut: " + (", ".join(f"{r['tier']}×{r['n']}" for r in dec) or "inga")]
        ev = await self.db.fetchall(
            "SELECT source, count(*) AS n FROM events WHERE created_at >= ? AND created_at < ? AND COALESCE(triaged_at, '') != 'baseline' "
            "GROUP BY source ORDER BY n DESC", (a, b)
        )
        out.append("Nya händelser per sinne: " + (", ".join(f"{r['source']}×{r['n']}" for r in ev) or "inga"))

        cr = await self.db.fetchall(
            "SELECT kind, count(*) AS n, sum(is_error) AS e, sum(duration_ms) / 1000 AS s FROM claude_runs "
            "WHERE created_at >= ? AND created_at < ? GROUP BY kind ORDER BY n DESC", (a, b)
        )
        out.append("Claude-körningar: " + (", ".join(f"{r['kind']}×{r['n']}" + (f" ({r['e']} fel)" if r["e"] else "") for r in cr) or "inga"))
        return "\n".join(out)

    async def runs(self, kind: str | None = None, n: int = 10) -> str:
        rows = await self.db.fetchall(
            "SELECT created_at, kind, model, duration_ms, is_error, error, trace, output FROM claude_runs "
            + ("WHERE kind = ? " if kind else "") + "ORDER BY id DESC LIMIT ?",
            ((kind, n) if kind else (n,)),
        )
        out = []
        for r in rows:
            trace = json.loads(r["trace"] or "[]")
            out.append(
                f"{r['created_at'][:16]} {r['kind']} ({r['model'] or '-'}, {(r['duration_ms'] or 0) / 1000:.0f}s)"
                + (f" FEL: {r['error'][:100]}" if r["is_error"] else "")
            )
            out += [f"    {'✗' if t['error'] else '·'} {t['tool']}: {t['input']}" for t in trace[:25]]
            if r["output"]:
                out.append(f"    → {r['output'][:200].replace(chr(10), ' ')}")
        return "\n".join(out) or "Inga körningar."

    async def decisions(self, n: int = 20) -> str:
        rows = await self.db.fetchall(
            "SELECT d.created_at, d.module, d.tier, d.reason, c.title FROM decisions d LEFT JOIN candidates c ON c.id = d.candidate_id "
            "ORDER BY d.id DESC LIMIT ?", (n,)
        )
        return "\n".join(f"{r['created_at'][:16]} {r['tier']:<14} {(r['title'] or '')[:50]:<50} {r['reason'][:90]}" for r in rows) or "Inga beslut."

    async def research(self) -> str:
        out = []
        for m in await self.db.fetchall("SELECT DISTINCT mission FROM scout_queries"):
            qs = await self.db.fetchall("SELECT query, status, runs FROM scout_queries WHERE mission = ? ORDER BY runs DESC", (m["mission"],))
            out.append(f"{m['mission']}: {sum(q['status'] == 'active' for q in qs)} aktiva frågor")
            out += [f"  [{q['status'][0]}] ×{q['runs']} {q['query'][:100]}" for q in qs[:12]]
        return "\n".join(out) or "Ingen sökportfölj än."

    async def senses(self) -> str:
        rows = await self.db.fetchall(
            "SELECT e.source, count(*) AS n, max(e.created_at) AS last, sum(c.status = 'sent') AS sent, "
            "sum(c.status IN ('shelved', 'pulled')) AS shelf, sum(c.status = 'dropped') AS dropped FROM events e "
            "LEFT JOIN candidates c ON c.event_id = e.id WHERE COALESCE(e.triaged_at, '') != 'baseline' "
            "AND e.created_at >= datetime('now', '-7 days') GROUP BY e.source ORDER BY n DESC"
        )
        out = ["Sinnen, 7 dagar (in → skickat / hylla / bort, senaste):"]
        out += [f"  {r['source']:<26} {r['n']:>3} → {r['sent'] or 0} / {r['shelf'] or 0} / {r['dropped'] or 0}   {r['last'][:16]}" for r in rows]
        web = await self.db.fetchall("SELECT domain, method, ok, fail, last_error FROM web_domains ORDER BY fail DESC LIMIT 10")
        if web:
            out += ["", "Läsning per domän (ok/fel):"] + [f"  {r['domain']:<30} {r['method']:<7} {r['ok']}/{r['fail']}" for r in web]
        return "\n".join(out)

    async def errors(self, n: int = 15) -> str:
        rows = await self.db.fetchall(
            "SELECT created_at, kind, error FROM claude_runs WHERE is_error = 1 ORDER BY id DESC LIMIT ?", (n,)
        )
        rr = await self.db.fetchall("SELECT created_at, research_id, error FROM research_runs WHERE error IS NOT NULL ORDER BY id DESC LIMIT 5")
        out = [f"{r['created_at'][:16]} {r['kind']}: {(r['error'] or '')[:140]}" for r in rows]
        out += [f"{r['created_at'][:16]} research {r['research_id']}: {r['error'][:140]}" for r in rr]
        return "\n".join(out) or "Inga fel loggade."

    async def summary(self) -> str:
        """For the chat: today in a few lines, plus health."""
        today = datetime.now(self.tz).date()
        a, b = self._bounds(today)
        sent = await self.db.fetchall(
            "SELECT created_at, text FROM messages WHERE origin = 'proactive' AND created_at >= ? AND created_at < ? ORDER BY id", (a, b)
        )
        dec = await self.db.fetchall(
            "SELECT tier, count(*) AS n FROM decisions WHERE created_at >= ? AND created_at < ? GROUP BY tier", (a, b)
        )
        ev = await self.db.fetchone(
            "SELECT count(*) AS n FROM events WHERE created_at >= ? AND created_at < ? AND COALESCE(triaged_at, '') != 'baseline'", (a, b)
        )
        runs = await self.db.fetchall("SELECT research_id, returned, findings FROM research_runs WHERE created_at >= ? AND created_at < ?", (a, b))
        problems = await self.health()
        lines = [
            "Hälsa: " + ("allt fungerar" if not problems else "; ".join(problems)),
            f"Nya händelser från sinnena: {ev['n']}",
            "Beslut: " + (", ".join(f"{r['tier']}×{r['n']}" for r in dec) or "inga"),
            "Undersökningar: " + (", ".join(f"{r['research_id']} ({r['returned']} fynd, {r['findings']} nya)" for r in runs) or "inga idag"),
            f"Skickat på eget initiativ: {len(sent)}",
        ]
        lines += [f"  {self._t(m['created_at'])} {m['text'][:90].replace(chr(10), ' ')}" for m in sent]
        return "\n".join(lines)


async def _main(args: list[str]) -> None:
    settings = load_settings()
    db = Database(settings.database)
    await db.open()
    i = Inspector(db, settings.timezone)
    cmd, rest = (args[0], args[1:]) if args else ("today", [])
    if cmd == "today":
        print(await i.day())
    elif cmd == "day":
        print(await i.day(date.fromisoformat(rest[0])))
    elif cmd == "runs":
        kind = rest[0] if rest and not rest[0].isdigit() else None
        n = int(next((x for x in rest if x.isdigit()), 10))
        print(await i.runs(kind, n))
    elif cmd == "decisions":
        print(await i.decisions(int(rest[0]) if rest else 20))
    elif cmd in ("research", "senses", "errors"):
        print(await getattr(i, cmd)())
    else:
        print(__doc__)
    await db.close()


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1:]))
