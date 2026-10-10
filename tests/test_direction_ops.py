import pytest
import yaml

from agent import direction_ops as ops


@pytest.fixture
def files(tmp_path, monkeypatch):
    research = tmp_path / "research.yaml"
    research.write_text("# behåll\n- id: jobs\n  goal: Hitta jobb\n  scope: Sthlm\n  interval_days: 3\n  enabled: true\n")
    sources = tmp_path / "sources.yaml"
    sources.write_text("streams:\n  - id: hn\n    type: rss\n    url: https://hn\n  # kommentar sist\n")
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text("# lista\ncompanies:\n  - {name: Spotify, ats: lever, slug: spotify}\n")
    for name, path in (("RESEARCH_FILE", research), ("SOURCES_FILE", sources), ("WATCHLIST_FILE", watchlist)):
        monkeypatch.setattr(ops, name, path)
    return research, sources, watchlist


def test_missions_add_update_disable(files):
    research, *_ = files
    changes = ops.apply_missions([
        {"op": "add", "id": "Partner Stockholm", "goal": "Hitta en affärspartner", "scope": "Uni", "reason": "tråd"},
        {"op": "update", "id": "jobs", "interval_days": 0.1, "reason": "brådis"},
        {"op": "disable", "id": "finns-inte", "reason": "x"},
    ])
    data = {r["id"]: r for r in yaml.safe_load(research.read_text())}
    assert data["partner-stockholm"]["goal"] == "Hitta en affärspartner"
    assert data["jobs"]["interval_days"] == 0.5  # sanity bound
    assert len(changes) == 2 and research.read_text().startswith("# behåll")
    ops.apply_missions([{"op": "disable", "id": "jobs", "reason": "klart"}])
    assert yaml.safe_load(research.read_text())[0]["enabled"] is False


def test_add_page_appends_stream_once(files):
    _, sources, _ = files
    assert "Bevakar" in ops.add_page("THS events", "https://ths/events", "Karriärevent")
    assert "redan" in ops.add_page("igen", "https://ths/events")
    streams = yaml.safe_load(sources.read_text())["streams"]
    assert [s["id"] for s in streams] == ["hn", "ths-events"] and streams[1]["type"] == "page"


async def test_add_company_detects_ats(files, monkeypatch):
    *_, watchlist = files

    async def fake_detect(url, client):
        return ("ashby", "acme")

    import agent.sources.connectors as conn

    monkeypatch.setattr(conn, "detect_ats", fake_detect)
    assert "bevakas nu (ashby)" in await ops.add_company("Acme", "https://acme/careers")
    assert "redan" in await ops.add_company("acme", "https://acme/careers")
    assert yaml.safe_load(watchlist.read_text())["companies"][-1] == {"name": "Acme", "careers_url": "https://acme/careers", "ats": "ashby", "slug": "acme"}
