"""Omgång 15, stage 1: what is watched works, entry by entry (the point it raises: tests/test_docket.py)."""

import pytest

from agent import watching
from agent.db import Database


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

    async def fixed(c, client):
        return []

    monkeypatch.setattr(connectors, "fetch_company_jobs", fixed)
    await connectors.poll_ats(config, None)
    await watching.record_entries(db, "watchlist-jobs")
    assert await watching.failing(db) == []   # read again without error: healthy
