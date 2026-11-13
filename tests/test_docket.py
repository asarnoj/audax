"""Omgång 16, stage 1: points that need an answer, the aging of the office's work, lessons where they are needed,
fields that hold where things stand, links the code makes, saved questions, the three parts and the measure."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from agent import docket, parts, points, routines, threads, trail
from agent.db import Database
from agent.forms import FormError
from agent.items import Items
from agent.office_tools import office_tools

TZ = ZoneInfo("Europe/Stockholm")


@pytest.fixture
async def db(tmp_path, monkeypatch):
    d = Database(tmp_path / "t.db")
    await d.open()
    trail.attach(d)
    monkeypatch.setattr(Items, "watchers", list(points.WATCHERS))
    yield d
    trail.attach(None)
    await d.close()


def text(out) -> str:
    return out["content"][0]["text"]


async def test_a_point_stays_until_it_is_answered_and_its_basis_is_written_in(db):
    pid = await docket.raise_point(db, "researcher", "x", "failing", "Bolaget går inte att läsa", "Fel: 404")
    assert await docket.raise_point(db, "researcher", "x", "failing", "Bolaget går inte att läsa", "Fel: 410") == pid   # one matter, one point
    shown = await docket.render(db, "researcher")
    assert f"D{pid}" in shown and "Fel: 410" in shown and "answer" in shown
    for _ in range(docket.SHOWN_TOO_OFTEN - 1):
        await docket.render(db, "researcher")
    assert f"D{pid} hos researcher" in await docket.unanswered(db)   # Manager sees what slides by
    with pytest.raises(ValueError):
        await docket.answer(db, "researcher", pid, "kept", "")   # a note is needed
    with pytest.raises(ValueError):
        await docket.answer(db, "director", pid, "kept", "ok")   # only its own
    await docket.answer(db, "researcher", pid, "later", "väntar på svar", later="2099-01-01")
    assert await docket.render(db, "researcher") == ""   # not before its day
    assert f"D{pid}" in await docket.render(db, "researcher", today=date(2099, 1, 2))
    await docket.answer(db, "researcher", pid, "struck", "struken")
    assert await docket.render(db, "researcher", today=date(2099, 1, 2)) == ""
    assert await docket.raise_point(db, "researcher", "x", "failing", "igen", quiet_days=3) == 0   # answered lately: not asked again at once


async def test_points_with_a_last_day_come_first_and_the_rest_wait_counted(db):
    await docket.raise_point(db, "director", "a", "aging", "Utan dag", "x" * 400, weight=5)
    await docket.raise_point(db, "director", "b", "lesson", "Med dag", "y", due="2026-11-20")
    shown = await docket.render(db, "director", budget=300)
    assert "Med dag" in shown and "Utan dag" not in shown and "1 punkt(er) till" in shown   # nothing dropped in silence


async def test_a_failing_watched_entry_is_a_point_that_resolves_when_it_reads_again(db, tmp_path, monkeypatch):
    from agent import watching
    from agent.sources import connectors
    from agent.sources.streams import StreamConfig

    (tmp_path / "w.yaml").write_text("companies:\n- name: Bra AB\n  ats: lever\n  slug: bra\n- name: Borta AB\n  ats: ashby\n  slug: borta\n")
    monkeypatch.setattr(connectors, "WATCHLIST", tmp_path / "w.yaml")

    async def fetch(c, client):
        if c.name == "Borta AB":
            raise RuntimeError("404 Not Found")
        return []

    monkeypatch.setattr(connectors, "fetch_company_jobs", fetch)
    config = StreamConfig(id="watchlist-jobs", type="ats", url="", params={"locations": ["Stockholm"], "roles": ["intern"]})
    await connectors.poll_ats(config, None)
    await watching.record_entries(db, "watchlist-jobs")
    [p] = await docket.open_points(db, "researcher")
    assert "Borta AB" in p["title"] and "404" in p["basis"] and "aldrig läst" in p["basis"]

    async def fixed(c, client):
        return []

    monkeypatch.setattr(connectors, "fetch_company_jobs", fixed)
    await connectors.poll_ats(config, None)
    await watching.record_entries(db, "watchlist-jobs")
    assert await docket.open_points(db, "researcher") == []   # reads again: the matter cleared by itself


async def test_when_the_owners_words_change_each_role_with_work_for_them_gets_one_point_beside_it(db):
    await routines.put_mission(db, {"id": "jobs", "goal": "Sommarjobb", "queries": ["QA OR cybersecurity intern"], "interval_days": 1}, "researcher")
    items = Items(db)
    await items.create("belief", "Inte intresserad av säkerhet", "assistant", kind="preference", source={"type": "owner", "quote": "inte säkerhet"})
    [p] = await docket.open_points(db, "researcher")
    assert p["kind"] == "aging" and "cybersecurity" in p["basis"] and "Inte intresserad av säkerhet" in p["basis"]
    assert await docket.open_points(db, "director") == []   # no work of its own that acts: nothing to ask
    await items.create("belief", "Vill ha deltid", "assistant", kind="preference", source={"type": "owner", "quote": "deltid"})
    [p] = await docket.open_points(db, "researcher")   # gathered in the open point, not a new one per change
    assert "Vill ha deltid" in p["basis"] and "säkerhet" in p["basis"] and "(2)" in p["title"]
    await items.create("belief", "Gillar korta svar", "director", kind="pattern", source={"type": "inferred"})
    assert "korta svar" not in (await docket.open_points(db, "researcher"))[0]["basis"]   # an interpretation is not the owner's words
    assert "[1] " in p["basis"]   # the work beside the words is numbered, to be judged line by line
    with pytest.raises(ValueError, match="off"):
        await docket.answer(db, "researcher", p["id"], "changed", "strök säkerhetsfrågan")   # which line?
    with pytest.raises(ValueError, match="name its numbers"):
        await docket.answer(db, "researcher", p["id"], "changed", "strök säkerhetsfrågan", off=[])
    await docket.answer(db, "researcher", p["id"], "changed", "strök säkerhetsfrågan", off=[1])
    assert (await docket.get(db, p["id"]))["answer"].startswith("Håller inte: 1.")
    assert await docket.open_points(db, "researcher") == []
    assert await points.seed_aging(db) == 2   # the first start sets the last week's words beside the work once more
    assert "deltid" in (await docket.open_points(db, "researcher"))[0]["basis"]


async def test_closing_an_experiment_asks_what_was_learned_and_the_lesson_meets_the_next_like_it(db, tmp_path):
    items = Items(db)
    eid = await items.create("experiment", "Söka på bolagens karriärsidor", "researcher", kind="improve",
                             data={"hypothesis": "fler praktikplatser", "intervention": "karriärsidor", "measure": "behållna fynd"},
                             review_at="2026-11-20")
    await items.update(eid, "researcher", status="dropped", data={"decision": "gav bara heltidsjobb"}, note="inget behölls")
    [p] = await docket.open_points(db, "researcher")
    assert p["kind"] == "lesson" and "gav bara heltidsjobb" in p["basis"] and "inget behölls" in p["basis"]
    tools = {t.name: t for t in office_tools(db, "researcher", memory=tmp_path)}
    out = text(await tools["answer"].handler({"id": f"D{p['id']}", "decision": "lesson", "note": "lärdom",
                                              "lesson": "Karriärsidor ger mest heltid; sommarpraktik kräver egna sökord"}))
    assert "Lesson kept as #U" in out
    lesson = (await items.find("belief", kind="practice"))[0]
    assert {l["to_id"] for l in await items.links(lesson["id"])} >= {eid}   # learned from the experiment
    out = text(await tools["create"].handler({"form": "experiment", "kind": "improve", "title": "Söka på karriärsidor igen",
                                              "fields": {"hypothesis": "h", "intervention": "i", "measure": "m"}, "review_at": "2026-12-01"}))
    assert "Tidigare prövat" in out and f"#E{eid}" in out and "gav bara heltidsjobb" in out


async def test_a_field_holds_where_things_stand_and_the_old_text_is_kept_in_the_history(db):
    items = Items(db)
    with pytest.raises(FormError, match="limit is 400"):
        await items.create("aim", "Mål", "director", kind="goal", data={"why": "x" * 500}, check_rights=True)
    gid = await items.create("aim", "Mål", "system", kind="goal", data={"why": "x" * 500}, owner="director")   # the engine's own writes are not held
    assert await points.sweep_oversized(db, "director") == 1
    [p] = await docket.open_points(db, "director")
    assert p["kind"] == "oversized" and "why 500" in p["basis"]
    with pytest.raises(FormError, match="give a note"):
        await items.update(gid, "director", data={"why": "Kort läge"}, check_rights=True)
    await items.update(gid, "director", data={"why": "Kort läge"}, note="historiken flyttad", check_rights=True)
    [h] = [h for h in await items.history(gid) if h["changes"] and "was" in h["changes"]]
    assert json.loads(h["changes"])["was"]["why"] == "x" * 500
    assert await docket.open_points(db, "director") == []   # within size again: resolved


async def test_the_code_links_what_a_text_names_by_id(db):
    items = Items(db)
    oid = await items.create("opportunity", "Praktik", "researcher", kind="job", data={"url": "https://x/1", "organization": "X"})
    gid = await items.create("aim", "Sommarjobb", "director", kind="goal", data={"about": f"deadline står på #O{oid}; inte #S{oid}"})
    assert [(l["to_id"], l["relation"]) for l in await items.links(gid)] == [(oid, "mentions")]   # the letter must match the form


async def test_a_saved_question_is_followed_and_shown_with_what_it_finds(db):
    items = Items(db)
    await items.create("opportunity", "Snart", "researcher", kind="job", data={"url": "https://x/1", "organization": "X",
                                                                               "deadline": f"{datetime.now():%Y-%m-%d}"})
    await items.create("opportunity", "Långt borta", "researcher", kind="job", data={"url": "https://x/2", "organization": "Y",
                                                                                     "deadline": "2099-01-01"})
    tools = {t.name: t for t in office_tools(db, "researcher")}
    out = text(await tools["follow"].handler({"name": "deadline snart", "question": {"form": "opportunity", "open": True, "within_days": 21},
                                              "why": "hinna söka"}))
    assert "Snart" in out and "Långt borta" not in out
    bad = text(await tools["follow"].handler({"name": "fel", "question": {"sql": "x"}, "why": "x"}))
    assert "Not saved" in bad
    review = await threads.for_review(db, "researcher", datetime.now(TZ))
    assert "deadline snart" in review and "Snart" in review
    await tools["follow"].handler({"name": "deadline snart", "stop": True, "why": "klart"})
    assert await threads.for_review(db, "researcher", datetime.now(TZ)) == ""


async def test_what_we_know_is_the_skills_areas_with_the_owners_words_first(db):
    items = Items(db)
    await items.create("belief", "Vill ha korta meddelanden", "assistant", kind="preference", data={"area": "communication"},
                       source={"type": "owner"})
    await items.create("belief", "Gillar spelutveckling", "director", kind="hypothesis", data={"area": "interests"}, source={"type": "inferred"})
    await items.create("belief", "Älskar ljud", "assistant", kind="preference", data={"area": "interests"}, source={"type": "owner"})
    deliver = await parts.known(db, "deliver")
    assert "korta meddelanden" in deliver and "spelutveckling" not in deliver   # the voice gets how the owner wants it
    search = await parts.known(db, "research")
    assert search.index("Älskar ljud") < search.index("spelutveckling") and "korta meddelanden" not in search


async def test_the_measure_counts_ids_shown_and_used(db):
    from agent import measure

    await db.execute("INSERT INTO claude_runs (kind, prompt, transcript) VALUES ('researcher_plan', ?, ?)",
                     ("Se #S5, #U31 och D7", json.dumps([{"tool": "office__read", "input": '{"id": "#S5"}'}, {"result": "#U99", "error": False}])))
    m = (await measure.runs(db, 1))["researcher_plan"]
    assert m["shown"] == 3 and m["used"] == 1 and m["fetches"] == 1
    assert "researcher_plan" in await measure.report(db, 1)


async def test_the_owners_words_are_set_beside_the_roles_own_interpretations_of_the_same_part_of_their_life(db):
    items = Items(db)
    guess = await items.create("belief", "Intressen: AI och cybersäkerhet", "assistant", kind="hypothesis", owner="assistant",
                               data={"area": "interests"}, source={"type": "inferred"})
    await items.create("belief", "Tre kontakter om dagen", "manager", kind="hypothesis", owner="manager",
                       data={"area": "interrupt"}, source={"type": "assumed"})
    await items.create("belief", "Inte intresserad av cybersäkerhet", "assistant", kind="preference",
                       data={"area": "not_interested"}, source={"type": "owner", "quote": "inte säkerhet"})
    [p] = await docket.open_points(db, "assistant")   # the interpretation's owner sees it beside the words
    assert f"#U{guess}" in p["basis"] and "Inte intresserad av cybersäkerhet" in p["basis"]
    assert await docket.open_points(db, "manager") == []   # another part of life: nothing to weigh


async def test_a_next_step_naming_what_has_closed_or_passed_is_a_point_once_per_state(db):
    items = Items(db)
    eid = await items.create("experiment", "En jam som ram", "director", kind="explore",
                             data={"hypothesis": "h", "intervention": "i", "measure": "m"}, review_at="2026-11-01")
    gid = await items.create("aim", "Spel med brodern", "director", kind="project", owner="director",
                             data={"next_step": f"Game Off delas före 1 nov som ram, se #E{eid}"})
    today = date(2026, 11, 12)
    assert await points.sweep_stale(db, "director", today) == 1
    [p] = [p for p in await docket.open_points(db, "director", today) if p["kind"] == "stale"]
    assert "1 Nov har passerat" in p["basis"] or "1 nov har passerat" in p["basis"].lower()
    await docket.answer(db, "director", p["id"], "kept", "gäller")
    assert await points.sweep_stale(db, "director", today) == 0   # answered for this state: not again
    await items.update(eid, "director", status="dropped", note="avbrutet")
    await points.sweep_stale(db, "director", today)
    [p] = [p for p in await docket.open_points(db, "director", today) if p["kind"] == "stale"]
    assert f"#E{eid} är dropped" in p["basis"]   # a new closed thing: asked again
    await items.update(gid, "director", data={"next_step": "Bygga i egen takt med brodern"}, note="ny riktning")
    await points.sweep_stale(db, "director", today)
    assert not [p for p in await docket.open_points(db, "director", today) if p["kind"] == "stale"]   # current again: resolved
    assert points.days_in("Armada 17–18 nov, ansökan 2027-01-15", today) == [date(2026, 11, 18), date(2027, 1, 15)]


async def test_the_keeper_of_the_owners_words_sees_their_older_words_a_new_one_may_replace(db):
    items = Items(db)
    old = await items.create("belief", "Får störas utom under föreläsningar", "assistant", kind="preference",
                             data={"area": "interrupt"}, source={"type": "owner", "quote": "utom föreläsningar"})
    other = await items.create("belief", "Gillar ljud", "assistant", kind="preference", data={"area": "interests"}, source={"type": "owner"})
    await db.execute("UPDATE items SET created_at = datetime('now', '-20 days') WHERE id IN (?, ?)", (old, other))
    await items.create("belief", "Audax får skriva när som helst; jag stänger av mobilen om jag inte vill bli störd", "assistant",
                       kind="preference", data={"area": "interrupt"}, source={"type": "owner", "quote": "när som helst"})
    [p] = await docket.open_points(db, "assistant")
    assert f"#U{old} Får störas utom under föreläsningar" in p["basis"] and "supersede" in p["basis"]
    assert f"#U{other}" not in p["basis"]   # another part of life
    assert await docket.open_points(db, "director") == []   # the owner's words are the Assistant's to keep
