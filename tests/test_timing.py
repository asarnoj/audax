"""Omgång 14, A: when the owner must know a finding at the latest; what is urgent follows from that and from now."""

import json
from datetime import datetime, time, timedelta

from agent import facit, ledger, timing
from agent.config import load_settings
from agent.items import Items
from agent.pipeline.outbox import add_candidate
from agent.schedule import utc_iso
from tests.test_pipeline import NOW, TZ, Clock, db, make_dispatcher  # noqa: F401  (db is a fixture)

QUIET = load_settings().quiet_hours


def test_a_bare_date_means_by_that_day_and_the_last_chance_is_before_the_night():
    assert timing.parse("2026-11-12 18:00", "Europe/Stockholm").startswith("2026-11-12T17:00")
    by_day = datetime.fromisoformat(timing.parse("2026-11-12", "Europe/Stockholm")).astimezone(TZ)
    assert (by_day.date().isoformat(), by_day.hour) == ("2026-11-12", 0)
    last = timing.last_chance(by_day, QUIET)
    assert last.date().isoformat() == "2026-11-11" and not QUIET.contains(last.time())
    noon = by_day.replace(hour=12)
    assert timing.last_chance(noon, QUIET) == noon
    assert timing.parse("snart", "Europe/Stockholm") is None and timing.parse(None, "Europe/Stockholm") is None


async def test_a_plan_past_the_moment_is_moved_to_it(db, tmp_path):
    # 9 Nov: the Grok meetup needed approval and had a waiting list, and was planned for the morning of the event
    by = NOW.replace(hour=12) + timedelta(days=1)
    g = await add_candidate(db, "event", "Meetup", "godkännande krävs", {"latest_why": "godkännande tar tid"}, latest_useful=utc_iso(by))
    night = await add_candidate(db, "event", "Ansökan", "x", {}, latest_useful=timing.parse("2026-10-15", "Europe/Stockholm"))
    d, _ = await make_dispatcher(db, tmp_path, {"decisions": [
        {"candidate_id": g, "decision": "at", "at": "2026-10-15 09:00", "reason": "samma morgon"},
        {"candidate_id": night, "decision": "at", "at": "2026-10-15 09:00", "reason": "på morgonen"}]})
    await d.run()
    planned = {r["id"]: r for r in await db.fetchall("SELECT id, deliver_at FROM candidates")}
    assert datetime.fromisoformat(planned[g]["deliver_at"]) == by
    evening = datetime.fromisoformat(planned[night]["deliver_at"]).astimezone(TZ)
    assert evening.date().isoformat() == "2026-10-14" and not QUIET.contains(evening.time())
    reason = await db.fetchone("SELECT tier FROM decisions WHERE candidate_id = ? ORDER BY id DESC", (g,))
    assert "senast nyttig" in reason["tier"]


async def test_what_is_saved_comes_back_once_when_its_moment_is_near(db, tmp_path):
    calls = []
    near = await add_candidate(db, "event", "Nära", "x", {}, latest_useful=utc_iso(NOW + timedelta(hours=20)))
    far = await add_candidate(db, "event", "Långt bort", "y", {}, latest_useful=utc_iso(NOW + timedelta(days=5)))
    await db.execute("UPDATE candidates SET status = 'shelved'")
    d, _ = await make_dispatcher(db, tmp_path, lambda t: {"decisions": [{"candidate_id": near, "decision": "shelf", "reason": "ljummet ändå"}]}, calls)
    await d.tick()
    assert len(calls) == 1 and "Ägaren behöver veta senast" in calls[0].prompt
    rows = {r["id"]: r["status"] for r in await db.fetchall("SELECT id, status FROM candidates")}
    assert rows == {near: "shelved", far: "shelved"}   # the desk let it lie, with a reason
    d._last_decide = None
    await d.tick()
    assert len(calls) == 1   # once per moment: it doesn't come back again
    await db.execute("UPDATE candidates SET latest_useful = ? WHERE id = ?", (utc_iso(NOW + timedelta(hours=10)), near))
    await d.tick()
    assert len(calls) == 2   # a check moved the moment earlier: judged again


async def test_what_the_owner_wants_together_waits_only_if_it_can(db, tmp_path):
    soon = await add_candidate(db, "event", "S", "kan inte vänta", {}, latest_useful=utc_iso(NOW + timedelta(hours=1)))
    later = await add_candidate(db, "event", "L", "kan vänta", {}, latest_useful=utc_iso(NOW + timedelta(days=3)))
    d, sent = await make_dispatcher(db, tmp_path, {"decisions": [{"candidate_id": c, "decision": "now", "reason": "x"} for c in (soon, later)]})
    d.settings.bundle_times, d.settings.binding = [time(17, 0)], {"quiet_hours", "bundle_times"}
    await d.run()
    assert sent == ["kan inte vänta"]
    assert (await db.fetchone("SELECT status FROM candidates WHERE id = ?", (later,)))["status"] == "scheduled"


async def test_holding_until_the_moment_passed_is_the_desks_error_and_counts_against_the_belief(db):
    items = Items(db)
    cap = await items.create("belief", "Hyllfynd högst ett om dagen", "manager", kind="practice", owner="manager", source={"type": "inferred"})
    c = await add_candidate(db, "event", "Meetup", "x", {}, latest_useful=utc_iso(datetime.now(TZ) - timedelta(hours=2)))
    await db.execute("UPDATE candidates SET status = 'shelved' WHERE id = ?", (c,))
    await db.insert("decisions", candidate_id=c, module="dispatcher@2", tier="sparad till senare", reason="ett om dagen")
    await facit.record(db, [f"#U{cap}"], c, "dispatcher@2", "shelf")
    e = await ledger.errors(db)
    assert e["let_pass"] == 1 and e["held_back"] == 1
    assert "lät tiden gå 1" in await ledger.summary(db, "Europe/Stockholm")
    await db.execute("UPDATE items SET updated_at = datetime('now', '-1 day') WHERE id = ?", (cap,))
    assert "hölls tills tiden gått" in await facit.for_review(db, "manager")
    sent_in_time = await add_candidate(db, "event", "I tid", "y", {}, latest_useful=utc_iso(datetime.now(TZ) - timedelta(hours=1)))
    await db.execute("UPDATE candidates SET status = 'sent', updated_at = datetime('now', '-3 hours') WHERE id = ?", (sent_in_time,))
    await db.insert("decisions", candidate_id=sent_in_time, module="dispatcher@2", tier="nu", reason="")
    assert (await ledger.errors(db))["let_pass"] == 1   # sent before its moment: no error


async def test_the_check_can_move_the_moment(db, tmp_path):
    from agent import checks

    c = await add_candidate(db, "event", "Meetup", "Grok-meetup 11 nov", {"check": "anmälan?"}, url="https://x",
                            latest_useful=utc_iso(NOW + timedelta(days=2)))
    d, _ = await make_dispatcher(db, tmp_path, {"confirmed": "Kräver godkännande, väntlista", "verdict": "keep", "why": "x",
                                                "latest_useful": "2026-10-13 12:00", "latest_why": "godkännandet tar tid"})
    row = await db.fetchone("SELECT * FROM candidates WHERE id = ?", (c,))
    assert await checks.check(db, d.gate, d.settings, None, row, "Julius", "2026-10-12")
    row = await db.fetchone("SELECT * FROM candidates WHERE id = ?", (c,))
    assert row["latest_useful"].startswith("2026-10-13T10:00") and json.loads(row["features"])["latest_why"] == "godkännandet tar tid"


async def test_a_finding_judged_before_the_owners_later_words_is_judged_again_before_it_goes(db, tmp_path):
    # 10 Nov: the OpenAI "rogue" retelling was kept 1 Nov, the owner said "already read" 4-5 Nov, it came back from the
    # shelf to the desk (which judges when, not what) and went out a third time
    calls = []
    for key in ("a", "b"):
        await db.execute("INSERT INTO events (source, kind, dedupe_key, payload) VALUES ('stream:lesswrong', 'event', ?, '{}')", (key,))
    rogue = await add_candidate(db, "story", "OpenAI rogue models retold", "x", {}, event_id=1)
    fresh_one = await add_candidate(db, "story", "Nvidia story", "y", {}, event_id=2)
    await db.execute("UPDATE candidates SET created_at = datetime('now', '-9 days')")
    await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (9, 'in', 'Jag har redan läst rogue-storyn om OpenAI')")
    await Items(db).create("belief", "Har redan läst rogue-storyn", "assistant", kind="fact", source={"type": "owner", "quote": "redan läst"})

    def answer(task):
        if task.kind == "triage":
            return {"items": [{"candidate_id": rogue, "keep": False, "reason": "Julius har redan läst den", "rests_on": []},
                              {"candidate_id": fresh_one, "keep": True, "reason": "fortfarande bra"}]}
        return {"decisions": [{"candidate_id": c, "decision": "now", "reason": "bra"} for c in (rogue, fresh_one)]}

    d, sent = await make_dispatcher(db, tmp_path, answer, calls)
    await d.run()
    assert sent == [] and [t.kind for t in calls] == ["dispatcher", "triage"]
    assert "redan läst rogue-storyn" in calls[1].prompt   # what the owner said about it, looked up
    status = {r["id"]: r["status"] for r in await db.fetchall("SELECT id, status FROM candidates")}
    assert status == {rogue: "dropped", fresh_one: "scheduled"}
    await d.tick()
    assert sent == ["y"] and len(calls) == 2   # judged once: what was kept goes, without asking again
    why = await db.fetchone("SELECT reason FROM decisions WHERE candidate_id = ? AND module = 'rejudge@1'", (rogue,))
    assert why["reason"] == "Julius har redan läst den"
