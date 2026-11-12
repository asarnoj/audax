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
    assert weight({"source": {"type": "owner"}}, "Kim") == "Kims egna ord"
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
    assert "policy.md: Max ca 3 proaktiva per dag." in text and "Inga Javajobb" not in text
    from agent import docket   # what cites the closed, or a belief, is a point set beside the words it cites (omgång 16)

    assert await freshness.cite_points(db, tmp_path, "manager") == 2
    points = {p["key"]: p for p in await docket.open_points(db, "manager")}
    assert f"#E{e} (dropped" in points["cite:arbetsminne/manager.md"]["basis"]
    policy = points["cite:policy.md"]
    assert f"#U{u} (current, ägarens egna ord): Kan inte Java" in policy["basis"]
    await docket.answer(db, "manager", policy["id"], "kept", "stämmer")
    assert await freshness.cite_points(db, tmp_path, "manager") == 1   # checked: not asked again
    (tmp_path / "policy.md").write_text(f"# Policy\n- Javajobb går bra (#U{u}).\n")
    assert await freshness.cite_points(db, tmp_path, "manager") == 2   # the line changed: asked again


def test_the_latest_week_is_read_against_the_owners_own_baseline():
    now = datetime(2026, 11, 9, 12)
    old = [{"local": now - timedelta(days=d), "answered": 1} for d in range(10, 20)]
    new = [{"local": now - timedelta(days=d), "answered": 0} for d in range(0, 3)]
    line = ledger.baseline(old + new, 21)
    assert "svar på 0 av 3 (0 %), mot 100 % av 10 dessförinnan" in line
    assert ledger.baseline(new, 21) == ""   # nothing to compare with


async def test_moving_the_lessons_with_the_view_watching_moves_each_once(db, tmp_path, monkeypatch):
    # 9 Nov: each belief made rewrote the view, which moved the same lesson again: 196 copies and a RecursionError
    async def watch(db_, item):
        if item["form"] == "belief":
            await write_practice(db_, tmp_path)

    monkeypatch.setattr(Items, "watchers", [watch])
    (tmp_path / "lardomar.md").write_text("# Lärdomar\n- A (Assistant, 18 okt)\n- B (Director, 2 nov)\n")
    await write_practice(db, tmp_path)
    assert [r["title"] for r in await Items(db).find("belief", kind="practice")] == ["A", "B"]


async def test_a_beliefs_record_brings_it_to_trial_when_it_points_against_it(db):
    # omgång 13, C: a cap in the policy held things back that the owner then fetched from the shelf
    from agent import facit
    from agent.pipeline.outbox import add_candidate

    items = Items(db)
    cap = await items.create("belief", "Högst ett hyllfynd om dagen", "manager", kind="practice", owner="manager",
                             data={"observations": "2026-10-29: ägaren bad om ping nära datum"}, source={"type": "inferred"})
    held = await add_candidate(db, "event", "AI-meetup", "x", {})
    sent = await add_candidate(db, "event", "RoyalRoppers", "y", {})
    await facit.record(db, [f"#U{cap}", "#U99999", "nonsense"], held, "dispatcher@2", "shelf")
    await facit.record(db, [f"U{cap}"], sent, "dispatcher@2", "now")
    await db.execute("UPDATE candidates SET status = 'sent' WHERE id = ?", (sent,))
    assert await facit.for_review(db, "manager") == ""   # nothing has pointed against it yet
    await db.execute("UPDATE items SET updated_at = datetime('now', '-1 hour') WHERE id = ?", (cap,))
    await db.insert("feedback", candidate_id=held, signal="shelf_pull", value="pulled")
    text = await facit.for_review(db, "manager")
    assert f"#U{cap}" in text and "höll tillbaka 1, varav 1 hämtades" in text and "skickade 1" in text
    assert "Facit: höll tillbaka 1" in await items.describe(cap)
    await items.update(cap, "manager", data={"observations": "2026-10-29: x\n2026-11-10: ändå hämtat; behåller med skäl"})
    assert await facit.for_review(db, "manager") == ""   # looked at since: it rests until something new points against it
