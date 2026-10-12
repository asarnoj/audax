"""Inspection: what the agent has done, decided and failed at. Read-only.

    uv run python -m agent.inspect [today|day YYYY-MM-DD|jobs|claude|runs [kind] [n]|decisions [n]|research|senses|errors]

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
        from agent.sources.streams import CONFIG_ERRORS

        problems = [f"Inställningsfilen {f} går inte att läsa ({err})" for f, err in CONFIG_ERRORS.items()]
        recent = await self.db.fetchall(
            "SELECT kind, is_error, error FROM claude_runs WHERE created_at >= datetime('now', '-2 hours') ORDER BY id DESC LIMIT 6"
        )
        if len(recent) >= 3 and all(r["is_error"] for r in recent[:3]):
            problems.append(f"Claude misslyckas upprepat ({recent[0]['kind']}: {(recent[0]['error'] or '')[:120]})")
        rl = await self.db.fetchone("SELECT rate_limit FROM claude_runs WHERE rate_limit IS NOT NULL ORDER BY id DESC LIMIT 1")
        if rl and json.loads(rl["rate_limit"]).get("status") == "rejected":
            problems.append("Claude-kvoten är slut, bakgrundsjobben väntar tills den återställs")
        breaker = await self.db.kv_get("outbox.breaker")
        if breaker and datetime.fromisoformat(breaker) > datetime.now(ZoneInfo("UTC")) - timedelta(hours=2):
            problems.append("Utskicken höll inne: ovanligt många meddelanden på en timme, något kan loopa (se audax decisions)")
        reported = await self.db.fetchall(
            "SELECT created_at, description FROM problems WHERE resolved_at IS NULL AND created_at >= datetime('now', '-7 days') ORDER BY id DESC LIMIT 3"
        )
        problems += [f"Chatten rapporterade {self._t(r['created_at'])}: {r['description'][:160]}" for r in reported]
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

    async def jobs(self) -> str:
        """What the engine is scheduled to do next: its own jobs, each sense, each mission."""
        from agent.research import load_research
        from agent.sources.streams import load_stream_configs

        now = datetime.now(self.tz)
        snap = await self.db.kv_get("engine.snapshot")
        out = []
        if not snap:
            out.append("Motorn har inte skrivit någon ögonblicksbild än (startar den?).")
        else:
            age = (now - datetime.fromisoformat(snap["at"])).total_seconds() / 60
            out.append(f"Motorn: pid {snap['pid']}, senaste livstecken för {age:.0f} min sedan" + (" ⚠️ INAKTUELL" if age > 12 else " ✓"))
            out.append("\nSchemalagda jobb (nästa körning):")
            for j in sorted(snap["jobs"], key=lambda j: j["next"] or "9"):
                when = datetime.fromisoformat(j["next"]).astimezone(self.tz).strftime("%a %d/%m %H:%M") if j["next"] else "-"
                out.append(f"  {when:<16} {j['id']:<22} {j['trigger']}")
        stats = {r["stream"]: r for r in await self.db.fetchall("SELECT * FROM stream_stats")}
        out.append("\nSinnen (senast kollat → nästa):")
        for c in load_stream_configs():
            if not c.enabled:
                out.append(f"  {'avstängt':<16} {c.id}")
                continue
            st = stats.get(c.id)
            if st and st["last_poll"]:
                last = datetime.fromisoformat(st["last_poll"] + "+00:00").astimezone(self.tz)
                nxt = last + timedelta(minutes=c.poll_minutes)
                out.append(f"  {nxt.strftime('%a %d/%m %H:%M'):<16} {c.id:<24} var {c.poll_minutes:g} min, senast {last:%H:%M}"
                           + (f", {st['errors']} fel" if st["errors"] else ""))
            else:
                out.append(f"  {'?':<16} {c.id:<24} var {c.poll_minutes:g} min, ingen körning registrerad än")
        out.append("\nSökuppdrag (nästa ordinarie → nästa djupsvep):")
        for t in load_research():
            last = await self.db.kv_get(f"research.last_run.{t.id}")
            deep = await self.db.kv_get(f"research.last_deep.{t.id}")
            nxt = (datetime.fromisoformat(last) + timedelta(days=t.interval_days)).astimezone(self.tz).strftime("%a %d/%m %H:%M") if last else "snarast"
            dnx = (datetime.fromisoformat(deep) + timedelta(days=7)).astimezone(self.tz).strftime("%a %d/%m") if deep else "när inget annat står på tur"
            out.append(f"  {nxt:<16} {t.id:<26} {'' if t.enabled else '(av) '}djupsvep: {dnx}")
        return "\n".join(out)

    async def claude(self, days: int = 7) -> str:
        """Every way the engine calls Claude (agent/calls.py) with what it cost the last week."""
        from agent.calls import CALLS
        from agent.config import load_settings

        models = load_settings().models
        rows = {
            r["kind"]: r
            for r in await self.db.fetchall(
                "SELECT kind, count(*) AS n, sum(is_error) AS e, avg(duration_ms) / 1000 AS avg_s, sum(duration_ms) / 1000 AS s, "
                "sum(cost_usd) AS usd FROM claude_runs WHERE created_at >= datetime('now', ?) GROUP BY kind",
                (f"-{days} days",),
            )
        }
        total = sum(r["usd"] or 0 for r in rows.values()) or 1
        out = [f"Claude-anrop, senaste {days} dagarna (API-ekvivalent kostnad = andel av kvoten):", ""]
        for kind, c in sorted(CALLS.items(), key=lambda kv: -(rows.get(kv[0], {"usd": 0})["usd"] or 0)):
            r = rows.get(kind)
            model = getattr(models, c.model.split(".")[1].split(" ")[0], c.model) if c.model.startswith("models.") else c.model
            stat = (f"{r['n']}×, {r['e'] or 0} fel, ~{r['avg_s']:.0f}s, ${r['usd'] or 0:.2f} ({100 * (r['usd'] or 0) / total:.0f}%)" if r else "ingen körning")
            ctx = await self.db.fetchone(
                "SELECT avg(context_tokens) AS a, max(context_tokens) AS m FROM claude_runs WHERE kind = ? AND context_tokens IS NOT NULL "
                "AND created_at >= datetime('now', ?)", (kind, f"-{days} days"),
            )
            if ctx and ctx["a"]:
                stat += f" · sammanhang ~{ctx['a'] / 1000:.0f}k tokens (max {ctx['m'] / 1000:.0f}k)"
            out += [f"{kind} [{c.role}, {model}] {stat}", f"    {c.purpose}", f"    prompt: {c.prompt} · verktyg: {c.tools} · när: {c.trigger}"]
        return "\n".join(out)

    async def usage(self, days: int = 7) -> str:
        """Where the subscription quota goes, and signs of waste. For analysis from the dev machine, not the chat."""
        since = f"-{days} days"
        q = self.db.fetchall
        out = [f"=== Användning, senaste {days} dagarna (kostnad = API-ekvivalent, ett mått på kvotandel) ==="]

        daily = await q(
            "SELECT date(created_at, 'localtime') AS d, count(*) AS n, sum(cost_usd) AS usd, "
            "sum(COALESCE(input_tokens,0) + COALESCE(cache_read_tokens,0) + COALESCE(cache_write_tokens,0)) AS tin, "
            "sum(output_tokens) AS tout FROM claude_runs WHERE created_at >= datetime('now', ?) GROUP BY d ORDER BY d", (since,)
        )
        total = sum(r["usd"] or 0 for r in daily) or 1
        out.append("\nPer dag:")
        peak = max((r["usd"] or 0) for r in daily) if daily else 1
        for r in daily:
            bar = "█" * max(1, int(20 * (r["usd"] or 0) / (peak or 1)))
            out.append(f"  {r['d']}  {bar:<20} ${r['usd'] or 0:6.2f}  {r['n']:>4} körningar" + (f", {(r['tin'] or 0) / 1e6:.1f}M tokens in" if r["tin"] else ""))

        kinds = await q(
            "SELECT kind, count(*) AS n, sum(cost_usd) AS usd, avg(cost_usd) AS avg_usd, avg(context_tokens) AS ctx, avg(turns) AS turns, "
            "sum(cache_read_tokens) AS cr, sum(cache_write_tokens) AS cw, sum(input_tokens) AS inp "
            "FROM claude_runs WHERE created_at >= datetime('now', ?) GROUP BY kind ORDER BY usd DESC", (since,)
        )
        out.append("\nPer sorts anrop (andel · per körning · sammanhang · steg · cacheträff):")
        for r in kinds:
            cache = (r["cr"] or 0) / ((r["cr"] or 0) + (r["cw"] or 0) + (r["inp"] or 0)) if (r["cr"] or r["cw"] or r["inp"]) else None
            out.append(
                f"  {r['kind']:<20} {100 * (r['usd'] or 0) / total:4.0f}%  {r['n']:>4}×  ${r['avg_usd'] or 0:.3f}/st"
                + (f"  ~{r['ctx'] / 1000:.0f}k" if r["ctx"] else "") + (f"  {r['turns']:.1f} steg" if r["turns"] else "")
                + (f"  cache {100 * cache:.0f}%" if cache is not None else "")
            )

        out.append("\nSignaler på onödig användning:")
        signals = []
        cold = await q(
            "SELECT count(*) AS n, sum(cost_usd) AS usd FROM claude_runs WHERE kind = 'chat' AND created_at >= datetime('now', ?) "
            "AND cache_write_tokens > 30000", (since,)
        )
        if cold and cold[0]["n"]:
            signals.append(f"Chatten fick bygga om cachen {cold[0]['n']} gånger (stort sammanhang + lång paus), ${cold[0]['usd'] or 0:.2f}. "
                           "Hålls nere av att sessionen startas om när den blir stor.")
        big = await q("SELECT max(context_tokens) AS m FROM claude_runs WHERE kind = 'chat' AND created_at >= datetime('now', ?)", (since,))
        if big and (big[0]["m"] or 0) > 60000:
            signals.append(f"Chattens största sammanhang: {big[0]['m'] / 1000:.0f}k tokens (gräns för omstart: 60k).")
        noop = await q(
            "SELECT count(*) AS n FROM claude_runs r WHERE r.kind = 'dispatcher' AND r.created_at >= datetime('now', ?) AND NOT EXISTS "
            "(SELECT 1 FROM decisions d WHERE d.created_at BETWEEN datetime(r.created_at, '-2 minutes') AND datetime(r.created_at, '+2 minutes') "
            "AND (d.tier LIKE '%(nu)%' OR d.tier IN ('shelf', 'drop')))", (since,)
        )
        disp = next((r for r in kinds if r["kind"] == "dispatcher"), None)
        if disp and noop and noop[0]["n"]:
            signals.append(f"Dispatchern: {noop[0]['n']} av {disp['n']} körningar ändrade inget (allt fick vänta).")
        empty = await q(
            "SELECT research_id, count(*) AS n FROM research_runs WHERE created_at >= datetime('now', ?) AND COALESCE(returned, findings) = 0 "
            "GROUP BY research_id", (since,)
        )
        for r in empty:
            signals.append(f"Sökuppdraget {r['research_id']}: {r['n']} körning(ar) utan ett enda fynd.")
        noisy = await q(
            "SELECT e.source, count(*) AS n, sum(c.status = 'dropped') AS dropped FROM events e JOIN candidates c ON c.event_id = e.id "
            "WHERE e.created_at >= datetime('now', ?) GROUP BY e.source HAVING n >= 5 ORDER BY dropped * 1.0 / n DESC", (since,)
        )
        for r in noisy:
            if (r["dropped"] or 0) / r["n"] >= 0.8:
                signals.append(f"{r['source']}: {r['dropped']} av {r['n']} bedömda saker sorterades bort. Triage lägger kvot på brus; "
                               "ett skarpare förfilter eller glesare takt skulle spara.")
        out += [f"  - {x}" for x in signals] or ["  inga"]

        top = await q(
            "SELECT datetime(created_at, 'localtime') AS t, kind, cost_usd, context_tokens, turns, duration_ms FROM claude_runs "
            "WHERE created_at >= datetime('now', ?) ORDER BY cost_usd DESC LIMIT 5", (since,)
        )
        out.append("\nDyraste enskilda körningar:")
        out += [f"  {r['t']}  {r['kind']:<18} ${r['cost_usd'] or 0:.2f}" + (f"  ~{r['context_tokens'] / 1000:.0f}k" if r["context_tokens"] else "")
                + (f"  {r['turns']} steg" if r["turns"] else "") + f"  {(r['duration_ms'] or 0) / 1000:.0f}s" for r in top]

        rl = await self.db.fetchone("SELECT rate_limit, datetime(created_at, 'localtime') AS t FROM claude_runs WHERE rate_limit IS NOT NULL ORDER BY id DESC LIMIT 1")
        if rl:
            info = json.loads(rl["rate_limit"])
            out.append(f"\nKvotfönster (senast {rl['t']}): {info.get('status')}, {info.get('type')}"
                       + (f", {100 * info['utilization']:.0f}% använt" if info.get("utilization") is not None else ", användning rapporteras inte"))
        return "\n".join(out)

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
    elif cmd == "usage":
        print(await i.usage(int(rest[0]) if rest else 7))
    elif cmd in ("research", "senses", "errors", "jobs", "claude"):
        print(await getattr(i, cmd)())
    else:
        print(__doc__)
    await db.close()


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1:]))
