"""What the search needs decided, as points (omgång 16, stage 2: instance/granskning/2026-11-12-omgang-16-kunskapen.md).

Researcher's planning prompt used to carry every source's yield, every mission's portfolio and map, and the test
cases, about 40 000 characters a night, of which it acted on a line or two. What asks for a decision is now a point,
with what is needed to decide it written in; the rest is fetched with `sources` and `mission` when wanted.

- a source that keeps little (noisy) or nothing (low yield) of what it brings, with what it brought lately;
- a mission whose queries ran a few times without anything kept, or whose portfolio is thin;
- a mission that runs and keeps nothing (quiet);
- a test case the search failed on: something that turned out to matter, missed or dropped.

A point whose matter clears (the source is narrowed and keeps more, the queries are replaced) resolves itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from agent import docket
from agent.db import Database
from agent.items import ref

LOOKED_AT = timedelta(days=7)    # a source its owner changed this recently is being worked on
QUIET_AFTER = timedelta(days=14)
AGAIN_AFTER = 7                  # days before a matter answered with "kept" is asked again, if it persists


async def sweep(db: Database, now: datetime) -> int:
    """Raises and resolves the search's points; returns how many are open after."""
    await _sources(db, now)
    await _missions(db, now)
    await _cases(db)
    r = await db.fetchone("SELECT count(*) AS n FROM docket WHERE role = 'researcher' AND status = 'open' AND "
                          "(key LIKE 'source:%' OR key LIKE 'queries:%' OR key LIKE 'quiet:%' OR key LIKE 'case:%')")
    return r["n"]


async def _sources(db: Database, now: datetime) -> None:
    from agent.due import sample, source_yields

    yields = await source_yields(db)
    total = sum(y["n"] for y in yields) or 1
    whole = 100 * sum(y["kept"] for y in yields) / total
    for y in yields:
        key = f"source:{y['id']}"
        if y["flag"] not in ("noisy", "low yield"):
            await docket.resolve(db, key, f"inte längre flaggad ({y['flag'] or 'ok'})", "researcher")
            continue
        if y["routine"]["updated_at"] >= f"{now - LOOKED_AT:%Y-%m-%d}":
            continue   # changed this week: give it time to show
        rows = await sample(db, y["id"])
        await docket.raise_point(
            db, "researcher", key, "source", f"Källan {y['id']} ({ref(y['routine'])}) är {y['flag']}",
            f"Senaste 30 dagarna: {y['n']} in, {y['kept']} behållna ({100 * y['kept'] / y['n']:.0f} %; hela inflödet behåller "
            f"{whole:.0f} %), {y['sent']} nådde ägaren.\nDet den gav senast, och vad det blev:\n" + "\n".join(f"- {r}" for r in rows)
            + "\nWhat do the kept differ from the rest by? Then, roughly in this order: narrow it at the source, filter "
            "with a rule, slow it, replace it with a mission, or pause it. Answer 'changed' with what you did, or 'kept' "
            "with why it is worth what it brings.", [ref(y["routine"])], quiet_days=AGAIN_AFTER)


async def _missions(db: Database, now: datetime) -> None:
    from agent import search_map
    from agent.research import BATCH, RETIRE_AFTER_RUNS, Portfolio, load_research

    for t in load_research():
        if not t.enabled:
            await docket.resolve(db, f"queries:{t.id}", "uppdraget är avstängt", "researcher")
            await docket.resolve(db, f"quiet:{t.id}", "uppdraget är avstängt", "researcher")
            continue
        stats = await Portfolio(db, None, None, None).stats(t.id, t.queries)
        stale = [q for q in stats if q["runs"] >= RETIRE_AFTER_RUNS and q["kept"] == 0]
        if stale or len(t.queries) < BATCH:
            good = sorted((q for q in stats if q not in stale), key=lambda q: (-q["kept"], q["runs"]))
            basis = (f"Portföljen har {len(t.queries)} frågor (minst {BATCH} behövs). "
                     + (f"Körda minst {RETIRE_AFTER_RUNS} gånger utan att något behållits (kept/runs · fråga):\n"
                        + "\n".join(docket.numbered([f"{q['kept']}/{q['runs']} \"{q['query']}\"" for q in stale]))
                        if stale else "")
                     + ("\nDe övriga: " + "; ".join(f"{q['kept']}/{q['runs']} \"{q['query']}\"" for q in good[:12]) if good else "")
                     + "\nReplace what doesn't pay with queries aimed at the gaps in the mission's map (read it with "
                     "`mission`); a query that opens a new kind of source is worth more than a variant. Name in `off` the "
                     "numbered ones you removed or replaced.")
            await docket.raise_point(db, "researcher", f"queries:{t.id}", "queries",
                                     f"Uppdraget {t.id}: frågor som inte ger något" if stale else f"Uppdraget {t.id}: för få frågor",
                                     basis, quiet_days=AGAIN_AFTER)
        else:
            await docket.resolve(db, f"queries:{t.id}", "portföljen ger", "researcher")
        from agent import parts

        if await parts.serves_none(db, t.id, f"{t.goal} {t.scope}"):
            await docket.raise_point(
                db, "researcher", f"serves:{t.id}", "serves", f"Uppdraget {t.id} är inte knutet till något mål",
                f"Mål: {t.goal[:400]}\nIts searches judge what they find against the goals it serves (why, what helps, what "
                "is noise), and this one names none, so they see the goals as titles only. Name the goal (#S…) it serves in "
                "its goal (`missions`, op update), or disable it if it serves none. Director's goals: find form=aim.",
                quiet_days=AGAIN_AFTER)
        else:
            await docket.resolve(db, f"serves:{t.id}", "knutet till ett mål", "researcher")
        st = await search_map.mission_stats(db, t.id)
        last = datetime.fromisoformat(st["last_kept"] + "+00:00") if st["last_kept"] else None
        if st["runs_30d"] >= 3 and (last is None or now - last >= QUIET_AFTER):
            await docket.raise_point(
                db, "researcher", f"quiet:{t.id}", "quiet", f"Uppdraget {t.id} hittar inget som behålls",
                f"Senaste 30 dagarna: {st['runs_30d']} körningar, {st['findings_30d']} fynd; senast behållet: "
                f"{st['last_kept'][:10] if st['last_kept'] else 'aldrig'}.\nWork out why before changing words: wrong places, "
                "the wrong time of year, or a scope too narrow (read the mission with `mission`). If it depends on how the "
                "owner sees it, find_out. Answer with what you changed, or 'later' with the day a season opens.",
                quiet_days=AGAIN_AFTER)
        else:
            await docket.resolve(db, f"quiet:{t.id}", "behåller igen", "researcher")


async def _cases(db: Database) -> None:
    from agent import cases

    await cases.collect(db)
    for c in await cases.unreviewed(db):
        what = ("aldrig sett av någon källa: en blind fläck. Find where it was published and which kind of method would "
                "have seen it, then add that method (a source, a query, a watch)." if c["status"] == "missed" else
                f"sett av {c['source']} men släppt: en bedömning, och triagen är Director's. Send Director a `report` "
                "with the case and why it was dropped, if the reason looks wrong.")
        await docket.raise_point(
            db, "researcher", f"case:{c['id']}", "case", f"Testfall: {c['title'][:90]}",
            f"Det här visade sig betyda något ({c['origin']}, {c['ref'] or ''}, {c['happened'] or '?'}): {c['title']}"
            + (f" {c['url']}" if c["url"] else "") + f"\nSökningen: {what}", weight=1)
        await db.execute("UPDATE search_cases SET reviewed_at = datetime('now') WHERE id = ?", (c["id"],))
