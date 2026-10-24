import pytest

from agent import questions, trail
from agent.db import Database
from agent.due import review
from agent.items import Items
from agent.office_tools import office_tools


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    trail.attach(d)
    Items.watchers = [questions.route]
    yield d
    Items.watchers = []
    trail.attach(None)
    await d.close()


def text(out):
    return out["content"][0]["text"]


async def test_a_question_to_the_world_becomes_a_step_and_its_result_the_answer(db):
    director = {t.name: t for t in office_tools(db, "director")}
    out = await director["find_out"].handler({"what": "Finns det improv-prova-på i Stockholm i november?", "why": "Julius vill börja",
                                              "guess": "Troligen, osäker", "settles_it": "Ett datum och en länk", "ask": "world"})
    assert "Researcher is on it" in text(out)
    q = (await Items(db).find("question"))[0]
    [step] = await Items(db).find("step", owner="researcher")
    assert q["status"] == "asked" and step["parent_id"] == q["id"] and step["done_when"] == "Ett datum och en länk"
    await Items(db).update(step["id"], "researcher", status="done", data={"result": "Drop-in varannan tisdag, https://x"})
    q = await Items(db).get(q["id"])
    assert q["status"] == "answered" and "Drop-in" in q["answer"]
    assert "Det du vill veta" not in await review(db, "director", __import__("datetime").datetime.now())


async def test_a_question_to_another_role_is_in_its_review_and_it_may_answer(db):
    manager = {t.name: t for t in office_tools(db, "manager")}
    await manager["find_out"].handler({"what": "Är Julius mitt i en tentaperiod nu?", "why": "Takten", "guess": "Nej", "ask": "director"})
    q = (await Items(db).find("question"))[0]
    from datetime import datetime

    assert f"#Q{q['id']} från manager" in await review(db, "director", datetime.now())
    assert f"#Q{q['id']} till director" in await review(db, "manager", datetime.now()) and "gissning: Nej" in await review(db, "manager", datetime.now())
    director = {t.name: t for t in office_tools(db, "director")}
    out = await director["update"].handler({"id": f"#Q{q['id']}", "status": "answered", "fields": {"answer": "Nej, tentorna är klara"}})
    assert "Not changed" not in text(out)


async def test_an_experiment_that_served_a_question_answers_it(db):
    researcher = {t.name: t for t in office_tools(db, "researcher")}
    await researcher["find_out"].handler({"what": "Ger Reddit-trådar om Stockholmsjobb något?", "why": "Ny sorts källa", "guess": "Kanske", "ask": "self"})
    q = (await Items(db).find("question"))[0]
    e = await Items(db).create("experiment", "Pröva r/stockholm", "researcher", kind="explore", owner="researcher", parent=q["id"],
                               review_at="2026-11-07", data={"hypothesis": "h", "intervention": "i", "measure": "m"})
    await Items(db).update(e, "researcher", status="dropped", data={"outcome": "Inget relevant på två veckor", "decision": "Släpp"})
    assert "Inget relevant" in (await Items(db).get(q["id"]))["answer"]
