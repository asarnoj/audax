"""Omgång 13, stage 1: what steers rests on a belief, a belief shows what it rests on, and what is old is shown as old."""

from datetime import datetime, timedelta

import pytest

from agent import freshness, ledger
from agent.beliefs import add_practice, practice_text, weight, write_practice
from agent.db import Database
from agent.items import Items


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


def test_a_belief_shows_how_much_it_rests_on():
    assert weight({"source": {"type": "owner"}}, "Frida") == "Fridas egna ord"
    assert weight({"source": {"type": "assumed"}}) == "ett antagande, aldrig bekräftat"
    assert weight({"source": {"type": "inferred", "evidence": "tre rättelser"}}) == "inget belägg angivet utom en fritext"
    obs = "2026-11-03: rättelse om seminariet (#M411)\n2026-11-05: Storträff\n2026-11-05: NYT-storyn"
    assert weight({"source": {"type": "inferred"}, "observations": obs}) == "vilar på 3 observationer, 3 nov–5 nov"


async def test_the_lessons_become_practice_beliefs_once_and_the_file_is_their_view(db, tmp_path):
    (tmp_path / "lardomar.md").write_text(
        "# Lärdomar\n\n<!-- x -->\n- Kontrollera datum innan något föreslås (Assistant, 18 okt)\n"
        "- Ett senare 'redan läst' väger tyngst (Manager, 6 nov). (Manager, 6 nov)\n")
    assert await write_practice(db, tmp_path)
    rows = await Items(db).find("belief", kind="practice")
    assert [(r["title"], r["owner"]) for r in rows] == [("Kontrollera datum innan något föreslås", "assistant"),
                                                        ("Ett senare 'redan läst' väger tyngst", "manager")]
    text = (tmp_path / "lardomar.md").read_text()
    assert text.startswith("# Kontorets praxis") and f"#U{rows[0]['id']}" in text and "vilar på 1 observation" in text
    assert not await write_practice(db, tmp_path)   # once: the view stays as it is
    await add_practice(db, tmp_path, "director", ["Läs repot i reflektionen"], datetime(2026, 11, 7))
    assert "Läs repot i reflektionen (Director; vilar på 1 observation, 7 nov)" in await practice_text(db)


async def test_old_references_and_rules_resting_on_nothing_are_shown(db, tmp_path):
    items = Items(db)
    e = await items.create("experiment", "Prövar buntar", "manager", kind="improve", status="running", owner="manager",
                           review_at="2026-11-09", data={"hypothesis": "h", "intervention": "i", "measure": "m"})
    u = await items.create("belief", "Kan inte Java", "assistant", kind="fact", source={"type": "owner", "quote": "Jag kan inte java"})
    await items.update(e, "manager", status="dropped")
    (tmp_path / "arbetsminne").mkdir()
    (tmp_path / "arbetsminne" / "manager.md").write_text(f"Trial #E{e} pågår till 9 nov.\n")
    (tmp_path / "policy.md").write_text(f"# Policy\n- Max ca 3 proaktiva per dag.\n- Inga Javajobb (#U{u}).\n")
    text = await freshness.for_review(db, tmp_path, "manager")
    assert f"#E{e} i arbetsminne/manager.md är dropped" in text
    assert "policy.md: Max ca 3 proaktiva per dag." in text and "Inga Javajobb" not in text


def test_the_latest_week_is_read_against_the_owners_own_baseline():
    now = datetime(2026, 11, 9, 12)
    old = [{"local": now - timedelta(days=d), "answered": 1} for d in range(10, 20)]
    new = [{"local": now - timedelta(days=d), "answered": 0} for d in range(0, 3)]
    line = ledger.baseline(old + new, 21)
    assert "svar på 0 av 3 (0 %), mot 100 % av 10 dessförinnan" in line
    assert ledger.baseline(new, 21) == ""   # nothing to compare with
