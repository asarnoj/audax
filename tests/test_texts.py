import pytest

from agent import texts
from agent.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


def test_a_section_is_changed_and_the_rest_of_the_file_stays(tmp_path):
    now = tmp_path / "direction" / "now.md"
    now.parent.mkdir()
    now.write_text("# Nu\n\n## Pågår\nTentavecka.\n\n## Skaver\nGoogle-ansökan.\n\n## Datum (skrivs av koden)\n- 2026-10-23: sista dag #S1\n")
    out = texts.edit(tmp_path, "director", "direction/now.md", "Pågår", "Tentan klar, flyttar i helgen.")
    assert "replaced" in out
    text = now.read_text()
    assert "Tentan klar" in text and "Tentavecka" not in text and "## Skaver\nGoogle-ansökan." in text   # nothing else lost
    texts.edit(tmp_path, "director", "direction/now.md", "Nytt", "Ett nytt avsnitt.")
    assert now.read_text().index("## Nytt") < now.read_text().index("## Datum (skrivs av koden)")   # before the code's own
    texts.edit(tmp_path, "director", "direction/now.md", "Skaver", "")
    assert "Skaver" not in now.read_text()


def test_only_the_owner_edits_and_never_what_the_code_writes(tmp_path):
    with pytest.raises(texts.TextError, match="manager's"):
        texts.edit(tmp_path, "director", "policy.md", "Läget", "x")
    with pytest.raises(texts.TextError, match="written by the code"):
        texts.edit(tmp_path, "director", "direction/now.md", "Datum (skrivs av koden)", "x")
    with pytest.raises(texts.TextError, match="not one of the texts"):
        texts.edit(tmp_path, "director", "profile.md", "Vem", "x")   # the profile is a view of the beliefs
    with pytest.raises(texts.TextError, match="only grows"):
        texts.edit(tmp_path, "manager", "lardomar.md", "Lärdomar", "x")
    with pytest.raises(texts.TextError):
        texts.edit(tmp_path, "researcher", "research/maps/../../x.md", "A", "x")
    assert texts.edit(tmp_path, "researcher", "research/maps/jobs.md", "Luckor och nästa steg", "- Myndigheter").startswith("research/maps/jobs.md")


def test_a_text_past_its_size_shows_in_its_owners_review(tmp_path):
    out = texts.edit(tmp_path, "manager", "arbetsminne/manager.md", "Fokus", "x" * 2500)
    assert "over its" in out
    assert texts.oversized(tmp_path, "manager") == ["arbetsminne/manager.md: 2521 chars, ~2000 is its size"]
    assert texts.oversized(tmp_path, "director") == []


async def test_the_office_text_tool(db, tmp_path):
    from agent.office_tools import office_tools

    tools = {t.name: t for t in office_tools(db, "manager", memory=tmp_path)}
    out = await tools["text"].handler({"file": "policy.md", "section": "Ton", "text": "Kort, svaret först."})
    assert "added" in out["content"][0]["text"] and "## Ton\nKort, svaret först." in (tmp_path / "policy.md").read_text()
    out = await tools["text"].handler({"file": "direction/person.md", "section": "Vem", "text": "x"})
    assert "Not changed" in out["content"][0]["text"]
    assert "text" not in {t.name for t in office_tools(db, "manager")}   # only with a memory to write in


async def test_the_old_portfolio_moves_into_the_missions_once(db):
    from agent import routines
    from agent.research import RUNS_KEY, load_research

    await routines.put_mission(db, {"id": "jobs", "goal": "Praktik"}, "system")
    for q, status, runs in (("sommarjobb IT", "active", 4), ("gammal", "retired", 9), ("trainee 2027", "active", 0)):
        await db.execute("INSERT INTO scout_queries (mission, query, status, runs) VALUES ('jobs', ?, ?, ?)", (q, status, runs))
    assert await routines.import_queries(db) == ["jobs"]
    await routines.refresh(db)
    assert load_research()[0].queries == ("sommarjobb IT", "trainee 2027")
    assert await db.kv_get(RUNS_KEY.format(id="jobs")) == {"sommarjobb IT": 4, "trainee 2027": 0}
    assert await routines.import_queries(db) == []   # once
