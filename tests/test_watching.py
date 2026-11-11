"""Omgång 15, stage 1: what is watched works, entry by entry, and the search's own texts age like findings."""

from zoneinfo import ZoneInfo

import pytest

from agent import routines, watching
from agent.db import Database
from agent.items import Items


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


async def test_each_company_on_the_watchlist_has_its_own_health(db, tmp_path, monkeypatch):
    # 11 Nov: one company's address went 404 and it was only a line in the log; the watch looked healthy
    from agent.sources import connectors
    from agent.sources.streams import StreamConfig

    (tmp_path / "w.yaml").write_text("companies:\n- name: Bra AB\n  ats: lever\n  slug: bra\n- name: Borta AB\n  ats: ashby\n  slug: borta\n")
    monkeypatch.setattr(connectors, "WATCHLIST", tmp_path / "w.yaml")

    async def fetch(c, client):
        if c.name == "Borta AB":
            raise RuntimeError("404 Not Found")
        return [("1", "Summer intern", "Stockholm", "https://x/1")]

    monkeypatch.setattr(connectors, "fetch_company_jobs", fetch)
    config = StreamConfig(id="watchlist-jobs", type="ats", url="", params={"locations": ["Stockholm"], "roles": ["intern"]})
    items = await connectors.poll_ats(config, None)
    assert [i.title for i in items] == ["Summer intern – Bra AB"]
    await watching.record_entries(db, "watchlist-jobs")
    [bad] = await watching.failing(db)
    assert bad["entry"] == "Borta AB" and "404" in bad["error"]
    text = await watching.entries_for_review(db, ZoneInfo("Europe/Stockholm"))
    assert "Borta AB (watchlist-jobs): RuntimeError: 404 Not Found" in text and "aldrig läst" in text and "Bra AB" not in text

    async def fixed(c, client):
        return []

    monkeypatch.setattr(connectors, "fetch_company_jobs", fixed)
    await connectors.poll_ats(config, None)
    await watching.record_entries(db, "watchlist-jobs")
    assert await watching.failing(db) == []   # read again without error: healthy


async def test_a_mission_written_before_the_owners_words_changed_is_shown_once_beside_them(db):
    # 11 Nov: a jobs query still searched "cybersecurity" after the owner said security isn't theirs
    await routines.put_mission(db, {"id": "jobs", "goal": "Sommarjobb", "queries": ["QA OR cybersecurity intern"], "interval_days": 1}, "researcher")
    await db.execute("UPDATE items SET updated_at = datetime('now', '-2 days') WHERE form = 'routine'")
    assert await watching.missions_for_review(db) == ""   # nothing the owner said has changed since
    await Items(db).create("belief", "Inte intresserad av säkerhet", "assistant", kind="preference", source={"type": "owner", "quote": "inte säkerhet"})
    text = await watching.missions_for_review(db)
    assert "jobs" in text and "cybersecurity" in text and "Inte intresserad av säkerhet" in text
    await watching.mark_looked(db)
    assert await watching.missions_for_review(db) == ""   # shown once per change
    await Items(db).create("belief", "Vill ha deltid", "assistant", kind="preference", source={"type": "owner", "quote": "deltid"})
    await db.execute("UPDATE items SET updated_at = datetime('now', '+1 minute') WHERE title = 'Vill ha deltid'")
    assert "Vill ha deltid" in await watching.missions_for_review(db)   # the owner's words changed again
    [(r, _)] = await watching.aged_missions(db)
    await routines.put_mission(db, {**r["_config"]["mission"], "queries": ["intern"]}, "researcher")
    await db.execute("UPDATE items SET updated_at = datetime('now', '+2 minutes') WHERE form = 'routine'")
    assert await watching.missions_for_review(db) == ""   # rewritten since: nothing to look at
