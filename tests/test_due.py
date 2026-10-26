from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent import trail
from agent.db import Database
from agent.due import Calendar, dates_text, review, write_now
from agent.items import Items

TZ = ZoneInfo("Europe/Stockholm")
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=TZ)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    trail.attach(d)
    yield d
    trail.attach(None)
    await d.close()


async def plan(db):
    items = Items(db)
    goal = await items.create("aim", "Medgrundare före jul", "director", kind="goal", data={"deadline": "2026-12-10"})
    research = await items.create("step", "Kolla att Collide går att gå på som individ", "director", owner="researcher", parent=goal,
                                  review_at="2026-10-20", data={"action": "research", "done_when": "Datum, länk, svar"})
    tell = await items.create("step", "Ta upp planen med Julius", "director", owner="assistant", parent=goal,
                              review_at="2026-10-20 11:30", data={"action": "tell_owner", "done_when": "Julius har fått planen"})
    later = await items.create("step", "Avstämning: vem har hen träffat?", "director", owner="director", parent=goal,
                               review_at="2026-12-15", data={"action": "check", "done_when": "Avstämt"})
    return goal, research, tell, later


async def test_the_calendar_wakes_the_owner_once_and_the_assistant_tells_the_owner(db):
    goal, research, tell, later = await plan(db)
    woken, spoken = [], []

    async def speak(instruction, about=frozenset()):
        spoken.append(instruction)
        return [1]

    async def never_quiet():
        return False

    cal = Calendar(db, "Europe/Stockholm", wake=lambda role, why: woken.append((role, why)), speak=speak, quiet=never_quiet)
    done = await cal.tick(NOW)
    assert woken == [("researcher", f"dags: #T{research}")]
    assert "Ta upp planen med Julius" in spoken[0] and "Medgrundare före jul" in spoken[0]
    assert (await Items(db).get(tell))["status"] == "done"
    assert len(done) == 2
    assert await cal.tick(NOW) == []   # once per date
    await Items(db).update(research, "researcher", status="waiting", review_at="2026-10-22", note="svar väntas")
    assert await cal.tick(NOW + timedelta(days=2)) == [f"#T{research} → researcher"]   # a new date is a new wake


async def test_quiet_hours_hold_what_would_reach_the_owner(db):
    _, _, tell, _ = await plan(db)

    async def quiet():
        return True

    cal = Calendar(db, "Europe/Stockholm", wake=lambda role, why: None, speak=None, quiet=quiet)
    await cal.tick(NOW)
    assert (await Items(db).get(tell))["status"] == "planned"   # later, when the owner can be reached


async def test_an_unanswered_question_is_closed_after_a_week(db):
    items = Items(db)
    q = await items.create("question", "Vad lockar med språkappen?", "director", kind="intent",
                           data={"to": "owner", "why": "förstå avsikten", "asked_at": "2026-10-10T12:00"}, status="asked")
    await Calendar(db, "Europe/Stockholm", wake=lambda r, w: None).tick(NOW)
    assert (await items.get(q))["status"] == "unanswered"


async def test_the_review_shows_what_is_due_running_waiting_and_the_balance(db):
    goal, research, tell, later = await plan(db)
    items = Items(db)
    await items.create("experiment", "Morgonbuntar", "manager", kind="improve", review_at="2026-10-27",
                       data={"hypothesis": "Färre avbrott", "intervention": "Samla tips till 08:30", "measure": "Svar och reaktioner"})
    await items.create("experiment", "Ett tillfälle utanför målen i veckan", "director", kind="explore", review_at="2026-11-03",
                       data={"hypothesis": "Öppnar något", "intervention": "Forskningspraktik", "measure": "Agerar hen?"})
    text = await review(db, "director", NOW)
    assert "Balans senaste tre veckorna: 0 förbättra, 1 utforska." in text and "Ett tillfälle utanför målen" in text
    assert "Avstämning" not in text   # not due yet
    researcher = await review(db, "researcher", NOW)
    assert "Dags nu" in researcher and "Kolla att Collide" in researcher


async def test_the_dates_in_now_md_come_from_the_items_and_survive_a_rewrite(db, tmp_path):
    await plan(db)
    path = tmp_path / "now.md"
    await write_now(db, path, NOW, "# Nu\nTentan klar.")
    text = path.read_text()
    assert text.startswith("# Nu\nTentan klar.") and "## Datum (skrivs av koden)" in text and "sista dag för #S1 Medgrundare före jul" in text
    await write_now(db, path, NOW, "# Nu\nNy text.\n\n## Datum (skrivs av koden)\n- gammalt som Director kopierade")
    assert "gammalt" not in path.read_text() and "Avstämning" in path.read_text()
    assert "2026-12-15" in await dates_text(db, NOW, days=60)


async def test_a_standing_order_shows_on_both_sides_until_withdrawn(db):
    from agent.posts import Posts

    posts = Posts(db)
    pid = await posts.send("director", "researcher", "order", "Ändra inget i sökningen förrän Julius svarat på #Q142")
    assert f"#P{pid} (order) till researcher" in await review(db, "director", NOW)
    assert f"#P{pid} (order) från director" in await review(db, "researcher", NOW) and "question it" in await review(db, "researcher", NOW)
    assert not await posts.withdraw(pid, "researcher", "inte min")   # only its sender withdraws it
    assert await posts.withdraw(pid, "director", "frös sökningen i veckor")
    assert f"#P{pid}" not in await review(db, "researcher", NOW)


async def test_feedback_that_tells_another_role_what_not_to_do_also_stands_until_its_date(db):
    from agent.posts import Posts

    pid = await Posts(db).send("director", "researcher", "feedback", "Ändra inget i sökningen förrän Julius har svarat på #Q142")
    assert f"#P{pid} (feedback) från director" in await review(db, "researcher", NOW)
    assert f"#P{pid} (feedback) till researcher" in await review(db, "director", NOW)
    assert await Posts(db).withdraw(pid, "director", "frös sökningen")


async def test_the_review_presses_an_overdue_experiment_and_shows_beliefs_that_may_have_changed(db):
    items = Items(db)
    e = await items.create("experiment", "Korta meddelanden", "manager", kind="improve", status="running", owner="manager",
                           review_at="2026-10-01", data={"hypothesis": "h", "intervention": "i", "measure": "m"})
    b = await items.create("belief", "Vill ha korta meddelanden", "manager", kind="preference", owner="manager",
                           source={"type": "observed"})
    await db.execute("UPDATE items SET updated_at = datetime('now', '-60 days') WHERE id = ?", (b,))
    text = await review(db, "manager", NOW)
    assert "long förbi" not in text and f"#E{e}" in text.split("Experiment långt förbi sitt datum")[1]
    assert f"#U{b}" in text.split("Uppfattningar som inte setts över")[1]


async def test_managers_overview_shows_how_the_contacts_landed(db):
    from agent.due import pulse

    await db.execute("INSERT INTO messages (telegram_message_id, direction, text, created_at) VALUES (1, 'in', 'hej', datetime('now', '-5 days'))")
    await db.execute("INSERT INTO messages (telegram_message_id, direction, text, origin, created_at) VALUES (2, 'out', 'Hur går det med barnboken?', 'proactive', datetime('now', '-3 days'))")
    text = await pulse(db, NOW)
    assert "Ägaren skrev senast: för 5 dagar sedan" in text and "Hur går det med barnboken?" in text and "inget svar" in text
