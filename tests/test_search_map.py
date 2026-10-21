import json

import pytest

from agent import search_map
from agent.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


async def test_leads_accumulate_by_place(db):
    leads = [{"name": "Thehub.io", "url": "https://thehub.io/jobs", "kind": "aggregator", "why": "startupjobb"},
             {"name": "Tibber", "url": None, "kind": "company", "why": "praktik"}]
    assert await search_map.record_leads(db, "jobs", leads) == 2
    assert await search_map.record_leads(db, "jobs", [{"name": "The Hub", "url": "https://www.thehub.io/", "kind": "aggregator", "why": "igen"}]) == 0
    row = await db.fetchone("SELECT id, times FROM leads WHERE key = 'thehub.io'")
    assert row["times"] == 2  # same host, seen twice: Researcher sees them as notes, the good ones become sources or steps


async def test_audit_checks_an_independent_benchmark_against_what_audax_reaches(db, tmp_path):
    from agent import routines

    watchlist = tmp_path / "w.yaml"
    watchlist.write_text("companies:\n  - name: Spotify\n  - name: Truecaller\n")
    await routines.put_stream(db, {"id": "jobtech", "type": "jobtech", "url": "https://jobsearch.api.jobtechdev.se"}, "system")
    await routines.put_mission(db, {"id": "jobs", "goal": "sommarpraktik"}, "system")
    await routines.refresh(db)
    await db.execute("INSERT INTO events (source, kind, dedupe_key, payload) VALUES ('stream:jobtech', 'job', 'k', ?)",
                     (json.dumps({"title": "Sommarpraktik – Ericsson AB"}),))
    summary = await search_map.audit(db, "jobs", ["Spotify", "Ericsson", "King", "Klarna (tech)"], "från kunskap", watchlist)
    assert summary == "täckning jobs: 2/4 (50%)"
    cov = await db.fetchone("SELECT * FROM coverage")
    assert json.loads(cov["missing"]) == ["King", "Klarna (tech)"]
    assert "Spotify (watched)" in json.loads(cov["covered"]) and "Ericsson (seen)" in json.loads(cov["covered"])


async def test_overview_and_gaps_come_from_the_map(db, tmp_path):
    p = search_map.map_path(tmp_path, "jobs")
    p.parent.mkdir(parents=True)
    p.write_text("# Sommarpraktik\n## Segment\n- Storbolag: bevakas\n## Luckor och nästa steg\n- Myndigheter: inget täcker dem\n")
    assert "Myndigheter" in await search_map.gaps(tmp_path, "jobs") and "Storbolag" not in await search_map.gaps(tmp_path, "jobs")
    await search_map.record_leads(db, "jobs", [{"name": "Varbi", "url": "https://varbi.com", "kind": "aggregator", "why": "myndigheter"}])
    text = await search_map.overview(db, tmp_path, "jobs")
    assert "Varbi" in text and "Coverage audit: none yet" in text and "Myndigheter" in text
