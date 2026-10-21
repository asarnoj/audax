import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.items import Items
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
    monkeypatch.setattr(ops, "WATCHLIST_FILE", watchlist)
    db = Database(tmp_path / "t.db")
    await db.open()
    from agent import routines

    await routines.import_files(db)
    await routines.refresh(db)
    Items.watchers = [routines.watch]
    await Items(db).create("aim", "Sommarpraktik 2027", "director", kind="goal", data={"about": "Brus: banker"}, source={"type": "inferred"})
    yield db, MemoryRepo(mem), research, sources
    Items.watchers = []
    await db.close()


def planner(db, memory, tmp_path, structured=None, calls=None):
    async def runner(task, ws, on_rl):
        if calls is not None:
            calls.append(task)
        return RunResult(text="", structured=structured)

    return ResearchPlanner(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), memory)


async def test_researcher_turns_an_order_into_missions_queries_sources_and_audits(env, tmp_path):
    db, memory, research, sources = env
    order = await Posts(db).send("director", "researcher", "order", "Ny beställning: hitta affärspartner", from_owner=True)
    started = []
    p = planner(db, memory, tmp_path)
    p.start_mission = started.append
    p._seen_posts = [order]
    out = {
        "missions": [{"op": "add", "id": "partner", "goal": "Hitta affärspartner för studentstartup", "scope": "Uni/Sthlm", "reason": "beställning"}],
        "queries": [{"mission": "jobs", "op": "add", "query": "site:varbi.se sommarjobb IT", "rationale": "myndigheter"},
                    {"mission": "jobs", "op": "add", "query": "trainee IT 2027", "rationale": "bredd"},
                    {"mission": "nope", "op": "add", "query": "x", "rationale": "okänt uppdrag"}],
        "senses": [{"op": "add_page", "name": "Startup events", "url": "https://sses/events", "note": "matchning", "reason": "partner"}],
        "coverage_audit": [{"mission": "jobs", "benchmark": ["sses", "Spotify", "King"], "note": "test"}],
        "deep_sweeps": ["jobs"],
        "run_now": ["partner"],
        "posts": [{"to": "director", "kind": "report", "body": "Partneruppdraget är igång"}],
        "owner_updates": [{"kind": "difficulty", "title": "Sommarpraktik", "brief": "Inget öppet än; de flesta öppnar i nov."}],
        "summary": "Satte upp partneruppdraget.",
    }
    changes = await p.apply(out, NOW)
    from agent.research import load_research
    from agent.sources.streams import load_stream_configs

    assert [t.id for t in load_research()] == ["jobs", "partner"]
    assert load_stream_configs()[-1].url == "https://sses/events"
    jobs = next(t for t in load_research() if t.id == "jobs")
    assert jobs.queries == ("site:varbi.se sommarjobb IT", "trainee IT 2027")   # the portfolio lives in the routine
    await p.apply({"queries": [{"mission": "jobs", "op": "remove", "query": "trainee IT 2027", "rationale": "gav inget"}]}, NOW)
    assert next(t for t in load_research() if t.id == "jobs").queries == ("site:varbi.se sommarjobb IT",)
    assert any(c.startswith("täckning jobs: 2/3") for c in changes)
    assert await db.kv_get("research.deep_requested.jobs") and started == ["partner"]
    assert [x["body"] for x in await Posts(db).inbox("director")] == ["Partneruppdraget är igång"]
    assert (await db.fetchone("SELECT kind FROM candidates"))["kind"] == "search_update"
    assert await Posts(db).inbox("researcher") == []  # the order was read


async def test_researcher_plans_when_needed_with_the_web_but_never_mail(env, tmp_path):
    db, memory, *_ = env
    calls = []
    empty = {"missions": [], "queries": [], "senses": [], "summary": ""}
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
    assert "Mål: Sommarpraktik 2027" in task.prompt and "Brus: banker" in task.prompt  # Director's brief
    assert await p.due() == "thin portfolio: jobs"  # no queries yet: plan again
    await p.apply({"queries": [{"mission": "jobs", "op": "add", "query": f"q{i}", "rationale": "r"} for i in range(6)]}, NOW)
    assert await p.due() is None
    await Posts(db).send("director", "researcher", "feedback", "för mycket storbolag")
    assert await p.due() == "inbox"
