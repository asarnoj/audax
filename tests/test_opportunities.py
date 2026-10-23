import pytest

from agent import opportunities, trail
from agent.db import Database
from agent.items import Items

LONDON = "https://www.google.com/about/careers/applications/jobs/results/100028133205254854-swe-sre-intern-2027"
EMEA = "https://www.google.com/about/careers/applications/jobs/results/142747733357142726-swe-sre-intern-2027"


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    trail.attach(d)
    yield d
    trail.attach(None)
    Items.watchers = []
    await d.close()


def finding(url, **kw):
    return {"title": "Software Engineering/SRE Intern 2027", "url": url, "kind": "opportunity", "organization": "Google",
            "checked": "page", "identity": "new", **kw}


async def test_two_postings_with_one_title_stay_two_and_a_copy_is_the_same(db):
    london, how = await opportunities.record(db, finding(LONDON, location="London", deadline="2026-10-23"))
    assert how == "new"
    emea, how = await opportunities.record(db, finding(EMEA, location="Zürich, München, Paris, Dublin, Stockholm", deadline="2026-10-23",
                                                      identity_note=f"Inte London-annonsen #O{london}: Stockholm valbar"))
    assert how == "new" and emea != london
    assert "London" in (await Items(db).get(emea))["differs_from"]
    copy, how = await opportunities.record(db, finding("https://jobs.example/google-swe-intern", identity="same", same_as=f"#O{emea}", checked="other_sites"))
    assert (copy, how) == (emea, "same")
    again, how = await opportunities.record(db, finding("https://jobs.example/google-swe-intern/", identity="new"))
    assert (again, how) == (emea, "address")   # the code holds it to the addresses, whatever the run said
    assert "London" in await opportunities.known(db) and "jobs.example" in await opportunities.known(db)


async def test_unclear_becomes_a_step_to_sort_out_and_a_page_check_corrects_a_weaker_one(db):
    first, _ = await opportunities.record(db, finding(LONDON, location="Stockholm", checked="other_sites"))
    other, how = await opportunities.record(db, finding(EMEA, identity="unclear", same_as=f"#O{first}", identity_note="Samma titel, annan id"))
    assert how == "unclear" and other != first
    [step] = await Items(db).find("step", owner="researcher")
    assert f"#O{other}" in step["title"] and f"#O{first}" in step["title"] and step["by"] == "system"
    await opportunities.record(db, finding(LONDON, location="London", checked="page"))
    o = await Items(db).get(first)
    assert o["place"] == "London" and o["checked"] == "page" and not o.get("unconfirmed")


async def test_what_the_owner_goes_for_keeps_its_opportunitys_dates(db):
    Items.watchers = [opportunities.follow]
    oid, _ = await opportunities.record(db, finding(EMEA, deadline="2026-10-23"))
    aim = await Items(db).create("aim", "Google-praktiken", "assistant", kind="pursuit", data={"opportunity": oid, "deadline": "2026-10-20"})
    await Items(db).update(oid, "researcher", data={"deadline": "2026-10-24"}, note="förlängd")
    a = await Items(db).get(aim)
    assert a["deadline"] == "2026-10-24" and a["url"] == EMEA


async def test_a_research_step_from_another_role_wakes_researcher_at_once(db):
    from agent.due import starter

    woken = []
    Items.watchers = [starter(lambda role, why: woken.append(role))]
    await Items(db).create("step", "Hitta improv-prova-på i Stockholm", "director", owner="researcher", review_at="2026-11-04",
                           data={"action": "research", "done_when": "Datum och länk"})
    assert woken == ["researcher"]
    await Items(db).create("step", "Mitt eget steg", "researcher", owner="researcher", review_at="2026-11-04",
                           data={"action": "research", "done_when": "x"})
    assert woken == ["researcher"]   # its own steps don't wake it again (no loop)
