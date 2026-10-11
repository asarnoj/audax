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


def test_add_topic_appends_baselined_topic_stream(files):
    _, sources, _ = files
    assert "Följer nyheter om Claude Code" in ops.add_topic("Claude Code", "Claude Code", "en")
    assert "redan" in ops.add_topic("Claude Code", "Claude Code")
    s = yaml.safe_load(sources.read_text())["streams"][-1]
    assert (s["id"], s["type"], s["params"]["query"]) == ("topic-claude-code", "topic", "Claude Code") and "baseline" not in s


def test_tune_and_disable_sense_keep_comments(files):
    _, sources, _ = files
    sources.write_text(
        "streams:\n  # nyheter\n  - id: hn\n    type: rss\n    url: https://hn\n    poll_minutes: 30\n"
        "  - id: dev\n    type: devpost\n  # sist\n"
    )
    assert ops.tune_sense("hn", 5, "lugn källa") == "hn: var 15 min"  # sanity bound
    assert ops.tune_sense("dev", 600) == "dev: var 600 min"
    assert "Hittar inget" in ops.tune_sense("nope", 60)
    assert ops.disable_sense("hn") == "hn: avstängt"
    text = sources.read_text()
    data = {s["id"]: s for s in yaml.safe_load(text)["streams"]}
    assert data["hn"]["poll_minutes"] == 15 and data["hn"]["enabled"] is False and data["dev"]["poll_minutes"] == 600
    assert "# nyheter" in text and "# sist" in text
