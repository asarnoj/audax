import json

import pytest

from agent import trail
from agent.db import Database
from agent.forms import FormError
from agent.items import Items, parse_ref
from agent.office_tools import describe_forms, office_tools
from agent.tools import TurnContext


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    trail.attach(d)
    yield d
    trail.attach(None)
    await d.close()


async def test_an_item_is_checked_against_its_form(db):
    items = Items(db)
    with pytest.raises(FormError) as e:
        await items.create("experiment", "Kortare meddelanden", "manager", kind="improve", data={"hypothesis": "Fler svar"})
    text = str(e.value)   # every problem at once, in words a role can act on
    assert "intervention is required" in text and "measure is required" in text and "review_at is required" in text
    with pytest.raises(FormError, match="source is required"):
        await items.create("belief", "Vill inte jobba på bank", "assistant", kind="preference")
    with pytest.raises(FormError, match="kind must be one of"):
        await items.create("belief", "x", "assistant", kind="hunch", source={"type": "owner"})
    with pytest.raises(FormError, match="no field 'colour'"):
        await items.create("step", "Kolla Collide", "researcher", data={"colour": "röd"}, review_at="2026-10-21")
    with pytest.raises(FormError, match="must be one of"):
        await items.create("step", "Kolla Collide", "researcher", data={"action": "phone", "done_when": "x"}, review_at="2026-10-21")


async def test_a_plan_is_steps_under_a_goal_with_owners_dates_and_history(db):
    items = Items(db)
    goal = await items.create("aim", "Medgrundare före jul", "director", kind="goal", source={"type": "owner", "message": 274})
    step = await items.create("step", "Kolla att Collide går att gå på som individ", "director", owner="researcher", parent=goal,
                              data={"action": "research", "done_when": "Datum, länk och svar på om man kan komma ensam"},
                              review_at="2026-10-21", note="Julius bad om en plan 19 okt")
    s = await items.get(step)
    assert (s["form"], s["status"], s["owner"], s["parent_id"], s["review_at"]) == ("step", "planned", "researcher", goal, "2026-10-21")
    assert await items.update(step, "researcher", status="done", data={"result": "Ja, 8 dec, öppet för alla"}, note="klart") == ["result", "status"]
    assert await items.update(step, "researcher", status="done") == []   # nothing changed, nothing logged
    hist = await items.history(step)
    assert [h["note"] for h in hist] == ["Julius bad om en plan 19 okt", "klart"]
    text = await items.describe(goal)
    assert "#S" + str(goal) in text and "Rymmer: #T" + str(step) in text and "Källa: type owner, message 274" in text
    assert [i["id"] for i in await items.find("step", open_only=True)] == []
    events = [r["kind"] for r in await db.fetchall("SELECT kind FROM office_events")]
    assert events.count("item") == 3


async def test_a_belief_can_replace_an_older_one_and_both_stay(db):
    items = Items(db)
    old = await items.create("belief", "Vill inte jobba på bank", "assistant", kind="preference",
                             data={"subject": "bank", "bubble": "excludes"}, source={"type": "owner", "message": 30, "date": "2026-10-09"})
    new = await items.create("belief", "Tekniska roller på bank går bra, inte rådgivning eller sälj", "assistant", kind="preference",
                             data={"subject": "bank"}, source={"type": "owner", "quote": "en teknisk roll på en bank kan jag faktiskt tänka mig"})
    await items.link(new, old, "supersedes", "assistant")
    await items.update(old, "assistant", status="superseded", note="Julius ändrade sig 19 okt")
    current = await items.find("belief", status="current", where="json_extract(data, '$.subject') = ?", args=("bank",))
    assert [c["id"] for c in current] == [new]
    assert "Länk (supersedes)" in await items.describe(old)


async def test_the_office_tools_check_rights_and_tell_the_role_what_is_wrong(db):
    turn = TurnContext()
    researcher = {t.name: t for t in office_tools(db, "researcher", turn)}
    director = {t.name: t for t in office_tools(db, "director")}
    out = await researcher["create"].handler({"form": "aim", "title": "x", "kind": "goal"})
    assert "researcher may not create a aim" in out["content"][0]["text"]   # goals are Director's
    out = await director["create"].handler({"form": "aim", "kind": "goal", "title": "Sommarjobb 2027", "fields": {"why": "Komma in i branschen"}})
    goal = parse_ref(out["content"][0]["text"].split()[2])
    out = await director["create"].handler({"form": "step", "title": "Armada-listan", "parent": f"#S{goal}", "owner": "researcher",
                                            "fields": json.dumps({"action": "research", "done_when": "Bolagen med sommarpraktik"}),
                                            "review_at": "2026-11-01"})
    step = parse_ref(out["content"][0]["text"].split()[2])
    out = await researcher["update"].handler({"id": f"#T{step}", "status": "done", "fields": {"result": "12 bolag"}, "note": "listan kom"})
    assert "changed: result, status" in out["content"][0]["text"] and f"#T{step}" in turn.refs
    out = await researcher["update"].handler({"id": f"#S{goal}", "status": "done"})   # not its goal
    assert "may not change" in out["content"][0]["text"]
    out = await director["find"].handler({"form": "step", "parent": f"#S{goal}"})
    assert "Armada-listan · done" in out["content"][0]["text"]
    out = await researcher["forms"].handler({})
    assert "#T step" in out["content"][0]["text"] and "#S aim" not in out["content"][0]["text"]   # only its own forms
    assert "#E experiment" in describe_forms()


async def test_the_aims_moved_in_with_their_numbers_and_history(tmp_path):
    """Migration 22 on a database as it looked on 19 Oct."""
    from agent.db import MIGRATIONS

    import aiosqlite

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        for i, sql in enumerate(MIGRATIONS[:21], start=1):
            await conn.executescript(sql)
            await conn.execute(f"PRAGMA user_version = {i}")
        await conn.execute("INSERT INTO pursuits (id, kind, title, status, deadline, url, source, why) VALUES "
                           "(1, 'pursuit', 'Google SRE', 'interested', '2026-10-23', 'https://g', 'chat', NULL), "
                           "(6, 'goal', 'Sommarjobb 2027', 'active', NULL, NULL, 'director', 'Komma in i branschen')")
        await conn.execute("UPDATE pursuits SET parent_id = 6 WHERE id = 1")
        await conn.execute("INSERT INTO pursuit_log (pursuit_id, status, note, by) VALUES (1, 'interested', 'lades till', 'chat')")
        await conn.commit()
    db = Database(path)
    await db.open()
    items = Items(db)
    google, goal = await items.get(1), await items.get(6)
    assert (google["form"], google["kind"], google["parent_id"], google["deadline"], google["url"]) == ("aim", "pursuit", 6, "2026-10-23", "https://g")
    assert google["source"] == {"type": "owner", "by": "chat"} and "why" not in google   # empty fields aren't carried over
    assert goal["why"] == "Komma in i branschen" and goal["source"]["type"] == "inferred"
    assert [h["note"] for h in await items.history(1)] == ["lades till"]
    assert await items.create("aim", "Nytt", "director", kind="idea") == 7   # the numbering goes on
    await db.close()
