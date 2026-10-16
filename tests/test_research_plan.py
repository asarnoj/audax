import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml

from agent import direction_ops as ops
from agent.claude_gate import ClaudeGate, RunResult
from agent.config import load_settings
from agent.db import Database
from agent.memory import MemoryRepo
from agent.posts import Posts
from agent.research_plan import ResearchPlanner

NOW = datetime(2026, 10, 14, 5, 45, tzinfo=ZoneInfo("Europe/Stockholm"))


@pytest.fixture
async def env(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    (mem / "direction").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=mem, check=True)
    from agent.config import instance_config

    research = instance_config("research.yaml")  # the same file the engine reads (isolated per test)
    research.write_text("- id: jobs\n  goal: Hitta praktik\n  scope: Sthlm\n  interval_days: 1\n  enabled: true\n")
    sources = tmp_path / "sources.yaml"
    sources.write_text("streams:\n  - id: hn\n    type: rss\n    url: https://hn\n")
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text("companies:\n  - name: Spotify\n")
    for name, path in (("RESEARCH_FILE", research), ("SOURCES_FILE", sources), ("WATCHLIST_FILE", watchlist)):
        monkeypatch.setattr(ops, name, path)
    db = Database(tmp_path / "t.db")
    await db.open()
    await db.execute("INSERT INTO pursuits (kind, title, status, about) VALUES ('goal', 'Sommarpraktik 2027', 'active', 'Brus: banker')")
    yield db, MemoryRepo(mem), research, sources
    await db.close()


def planner(db, memory, tmp_path, structured=None, calls=None):
    async def runner(task, ws, on_rl):
        if calls is not None:
            calls.append(task)
        return RunResult(text="", structured=structured)

    return ResearchPlanner(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), memory)


async def test_researcher_turns_an_order_into_missions_queries_sources_maps_and_audits(env, tmp_path):
    db, memory, research, sources = env
    order = await Posts(db).send("director", "researcher", "order", "Ny beställning: hitta affärspartner", from_owner=True)
    await db.execute("INSERT INTO leads (mission, key, name, kind) VALUES ('jobs', 'varbi.com', 'Varbi', 'aggregator')")
    started = []
    p = planner(db, memory, tmp_path)
    p.start_mission = started.append
    p._seen_posts = [order]
    out = {
        "missions": [{"op": "add", "id": "partner", "goal": "Hitta affärspartner för studentstartup", "scope": "Uni/Sthlm", "reason": "beställning"}],
        "queries": [{"mission": "jobs", "query": "site:varbi.se sommarjobb IT", "rationale": "myndigheter"},
                    {"mission": "nope", "query": "x", "rationale": "okänt uppdrag"}],
        "senses": [{"op": "add_page", "name": "Startup events", "url": "https://sses/events", "note": "matchning", "reason": "partner"}],
        "search_maps": [{"mission": "jobs", "content_md": "# Jobb\n## Luckor och nästa steg\n- Myndigheter"}, {"mission": "../../evil", "content_md": "x"}],
        "lead_decisions": [{"id": 1, "decision": "add", "reason": "myndigheter annonserar där"}],
        "coverage_audit": [{"mission": "jobs", "benchmark": ["sses", "Spotify", "King"], "note": "test"}],
        "deep_sweeps": ["jobs"],
        "run_now": ["partner"],
        "posts": [{"to": "director", "kind": "report", "body": "Partneruppdraget är igång"}],
        "owner_updates": [{"kind": "difficulty", "title": "Sommarpraktik", "brief": "Inget öppet än; de flesta öppnar i nov."}],
        "working_memory_md": "Fokus: partneruppdraget.",
        "summary": "Satte upp partneruppdraget.",
    }
    changes = await p.apply(out, NOW)
    assert [r["id"] for r in yaml.safe_load(research.read_text())] == ["jobs", "partner"]
    assert yaml.safe_load(sources.read_text())["streams"][-1]["url"] == "https://sses/events"
    assert [r["query"] for r in await db.fetchall("SELECT query FROM scout_queries")] == ["site:varbi.se sommarjobb IT"]
    assert (memory.path / "research" / "maps" / "jobs.md").read_text().startswith("# Jobb")
    assert not (memory.path.parent / "evil.md").exists()
    assert (await db.fetchone("SELECT status FROM leads WHERE id = 1"))["status"] == "added"
    assert any(c.startswith("täckning jobs: 2/3") for c in changes)
    assert await db.kv_get("research.deep_requested.jobs") and started == ["partner"]
    assert [x["body"] for x in await Posts(db).inbox("director")] == ["Partneruppdraget är igång"]
    assert (await db.fetchone("SELECT kind FROM candidates"))["kind"] == "search_update"
    assert (memory.path / "arbetsminne" / "researcher.md").read_text() == "Fokus: partneruppdraget.\n"
    assert await Posts(db).inbox("researcher") == []  # the order was read


async def test_researcher_plans_when_needed_with_the_web_but_never_mail(env, tmp_path):
    db, memory, *_ = env
    calls = []
    empty = {"missions": [], "queries": [], "senses": [], "lead_decisions": [], "working_memory_md": "Fokus.", "summary": ""}
    p = planner(db, memory, tmp_path, structured=empty, calls=calls)

    class Web:
        async def stats(self):
            return ""

    p.web = Web()
    assert await p.due() == "weekly"  # never planned
    await p.run_if_due()
    [task] = calls
    assert task.kind == "researcher_plan" and "mcp__web__web_read" in task.allowed_tools
    assert not any("mail" in t for t in task.allowed_tools)
    assert "#S1 Mål: Sommarpraktik 2027" in task.prompt and "Brus: banker" in task.prompt  # Director's brief
    assert await p.due() == "thin portfolio: jobs"  # no queries yet: plan again
    for i in range(6):
        await db.execute("INSERT INTO scout_queries (mission, query) VALUES ('jobs', ?)", (f"q{i}",))
    assert await p.due() is None
    await Posts(db).send("director", "researcher", "feedback", "för mycket storbolag")
    assert await p.due() == "inbox"
