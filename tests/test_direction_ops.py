import pytest
import yaml

from agent import direction_ops as ops
from agent import routines
from agent.config import instance_config
from agent.db import Database
from agent.items import Items
from agent.sources.streams import load_stream_configs
from agent.research import load_research


@pytest.fixture
async def db(tmp_path, monkeypatch):
    instance_config("research.yaml").write_text("- id: jobs\n  goal: Hitta jobb\n  scope: Sthlm\n  interval_days: 3\n  enabled: true\n")
    instance_config("sources.yaml").write_text(
        "calendars:\n  sources: []\nstreams:\n  - id: hn\n    type: rss\n    url: https://hn\n    poll_minutes: 30\n"
        "  - id: dev\n    type: devpost\n    enabled: false\n")
    watchlist = instance_config("watchlist.yaml")
    watchlist.write_text("# lista\ncompanies:\n  - {name: Spotify, ats: lever, slug: spotify}\n")
    monkeypatch.setattr(ops, "WATCHLIST_FILE", watchlist)
    d = Database(tmp_path / "t.db")
    await d.open()
    Items.watchers = [routines.watch]
    await routines.import_files(d)
    await routines.refresh(d)
    yield d
    Items.watchers = []
    await d.close()


async def test_the_files_are_moved_once_and_kept_as_imported(db):
    assert [c.id for c in load_stream_configs()] == ["hn"]   # dev was switched off: paused, not running
    assert [t.id for t in load_research()] == ["jobs"]
    assert instance_config("sources.yaml.imported").exists() and instance_config("research.yaml.imported").exists()
    assert yaml.safe_load(instance_config("sources.yaml").read_text()) == {"calendars": {"sources": []}}   # the rest stays
    dev = await routines.find(db, "stream", "dev")
    assert dev["status"] == "paused" and dev["owner"] == "researcher"
    assert await routines.import_files(db) == []   # once
    from agent.config import ensure_instance

    ensure_instance()   # the next start doesn't bring the moved files back from the examples
    assert not instance_config("research.yaml").exists()


async def test_missions_add_update_disable(db):
    changes = await ops.apply_missions(db, [
        {"op": "add", "id": "Partner Stockholm", "goal": "Hitta en affärspartner", "scope": "Uni", "reason": "tråd"},
        {"op": "update", "id": "jobs", "interval_days": 0.1, "reason": "brådis"},
        {"op": "disable", "id": "finns-inte", "reason": "x"},
    ])
    data = {t.id: t for t in load_research()}
    assert data["partner-stockholm"].goal == "Hitta en affärspartner" and data["jobs"].interval_days == 0.5   # sanity bound
    assert len(changes) == 2
    await ops.apply_missions(db, [{"op": "disable", "id": "jobs", "reason": "klart"}])
    assert {t.id: t for t in load_research()}["jobs"].enabled is False
    hist = await Items(db).history((await routines.find(db, "mission", "jobs"))["id"])
    assert [h["note"] for h in hist][-2:] == ["brådis", "klart"]   # every change with its reason


async def test_add_page_and_topic_start_running_and_are_added_once(db):
    restarted = []

    async def restart():
        restarted.append(1)

    routines.on_streams_changed.append(restart)
    assert "Bevakar" in await ops.add_page(db, "THS events", "https://ths/events", "Karriärevent")
    assert "redan" in await ops.add_page(db, "igen", "https://ths/events")
    assert "Följer nyheter om Claude Code" in await ops.add_topic(db, "Claude Code", "Claude Code", "en", urgent=True)
    assert "redan" in await ops.add_topic(db, "Claude Code", "Claude Code")
    configs = {c.id: c for c in load_stream_configs()}
    assert configs["ths-events"].type == "page" and configs["topic-claude-code"].params["query"] == "Claude Code"
    assert configs["topic-claude-code"].urgent and not configs["ths-events"].urgent
    assert len(restarted) == 2   # the pollers follow at once


async def test_tune_and_disable(db):
    assert await ops.tune_sense(db, "hn", 5, "lugn källa") == "hn: var 15 min"   # sanity bound
    assert "Hittar inget" in await ops.tune_sense(db, "nope", 60)
    assert {c.id: c for c in load_stream_configs()}["hn"].poll_minutes == 15
    assert await ops.disable_sense(db, "hn", "brus") == "hn: avstängt"
    assert load_stream_configs() == []
    assert (await routines.find(db, "stream", "hn"))["status"] == "paused"


async def test_add_company_detects_ats(db, monkeypatch):
    async def fake_detect(url, client):
        return ("ashby", "acme")

    import agent.sources.connectors as conn

    monkeypatch.setattr(conn, "detect_ats", fake_detect)
    assert "bevakas nu (ashby)" in await ops.add_company(db, "Acme", "https://acme/careers")
    assert "redan" in await ops.add_company(db, "acme", "https://acme/careers")
    assert yaml.safe_load(instance_config("watchlist.yaml").read_text())["companies"][-1] == {
        "name": "Acme", "careers_url": "https://acme/careers", "ats": "ashby", "slug": "acme"}


async def test_a_noisy_source_is_flagged_beside_the_whole_intake_shown_and_narrowed_with_its_history(db, monkeypatch):
    from agent.due import source_yield, source_yields

    await routines.put_stream(db, {"id": "quiet", "type": "rss", "url": "https://q"}, "researcher")
    for source, n, kept in (("hn", 40, 1), ("quiet", 20, 10)):   # the intake keeps 11 of 60; hn keeps 1 of 40
        for i in range(n):
            e = await db.execute("INSERT INTO events (source, kind, dedupe_key, payload) VALUES (?, 'news', ?, '{}')", (f"stream:{source}", f"{source}{i}"))
            c = await db.execute("INSERT INTO candidates (event_id, kind, title, body, status) VALUES (?, 'news', ?, '', ?)",
                                 (e, f"{source} {i}", "shelved" if i < kept else "dropped"))
            await db.execute("INSERT INTO decisions (candidate_id, module, tier, reason) VALUES (?, 'dispatcher@1', 'drop', 'bara brus')", (c,))
    flags = {y["id"]: y["flag"] for y in await source_yields(db)}
    assert flags["hn"] == "noisy" and flags["quiet"] == ""
    text = await source_yield(db)
    assert "Senast från hn (noisy)" in text and "dropped: hn 39 (bara brus)" in text

    async def fetched(type_, url, lang="en"):
        return "Kontrollerat: flödet fungerar, 10 inlägg." if "top" in url else "Varning: källan svarade 404."

    monkeypatch.setattr(ops, "verify_source", fetched)
    assert "användes inte" in await ops.narrow_sense(db, "hn", "https://hn/missing", None, "smalare")
    assert "ny adress" in await ops.narrow_sense(db, "hn", "https://hn/top", "Bara det högt röstade", "smalare")
    hn = await routines.find(db, "stream", "hn")
    assert hn["_config"]["stream"]["url"] == "https://hn/top" and hn["_config"]["stream"]["note"] == "Bara det högt röstade"
    assert [c.url for c in load_stream_configs() if c.id == "hn"] == ["https://hn/top"]   # what runs follows
