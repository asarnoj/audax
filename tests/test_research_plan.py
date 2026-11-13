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
        "thoughts": [{"kind": "working_on", "title": "Sommarpraktik", "thought": "Inget öppet än; de flesta öppnar i nov.",
                      "why_them": "så du vet varför det är tyst"}],
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
    assert (await db.fetchone("SELECT kind FROM candidates"))["kind"] == "thought"
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
    assert "Sommarpraktik 2027" in task.prompt and "Brus: banker" not in task.prompt  # the goal's line; its brief is read when wanted
    from agent import docket

    assert "Uppdraget jobs: för få frågor" in task.prompt   # no queries yet: a point, in the run
    assert await p.due() is None   # just planned
    await p.apply({"queries": [{"mission": "jobs", "op": "add", "query": f"q{i}", "rationale": "r"} for i in range(6)]}, NOW)
    assert await p.due() is None
    assert not [x for x in await docket.open_points(db, "researcher") if x["key"] == "queries:jobs"]   # cleared by itself
    await Posts(db).send("director", "researcher", "feedback", "för mycket storbolag")
    assert await p.due() == "inbox"


async def test_every_open_step_gets_an_outcome_written_on_it(env, tmp_path):
    db, memory, *_ = env
    items = Items(db)
    pottery = await items.create("step", "Keramik-prova-på i Stockholm", "director", owner="researcher", review_at="2026-11-04",
                                data={"action": "research", "done_when": "Datum och länk"})
    armada = await items.create("step", "Armada-utställare", "director", owner="researcher", review_at="2026-11-06",
                                data={"action": "research", "done_when": "Lista"})
    p = planner(db, memory, tmp_path)
    changes = await p.apply({"steps": [{"id": f"#T{pottery}", "outcome": "done", "result": "Drop-in varannan tisdag, https://x"}]}, NOW)
    assert (await items.get(pottery))["status"] == "done" and "Drop-in" in (await items.get(pottery))["result"]
    assert f"steg utan åtgärd: #T{armada}" in changes   # left out: named in the run's account


async def test_a_noisy_source_not_looked_at_in_a_week_is_a_point_that_wakes_researcher(env, tmp_path, monkeypatch):
    from datetime import timedelta

    import agent.due as due_mod
    import agent.research_plan as rp
    from agent import docket

    db, memory, research, sources = env
    p = planner(db, memory, tmp_path)
    await db.kv_set("researcher.last_plan", (datetime.now(ZoneInfo("Europe/Stockholm")) - timedelta(days=4)).isoformat())
    monkeypatch.setattr(rp, "load_research", lambda: [])   # no mission asks for a run
    monkeypatch.setattr("agent.research.load_research", lambda: [])
    seen = {"id": 7, "form": "routine", "updated_at": datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")}

    async def yields(db_, days=30):
        return [{"routine": seen, "id": "hn", "n": 40, "kept": 1, "sent": 0, "flag": "noisy"}]

    monkeypatch.setattr(due_mod, "source_yields", yields)
    assert await p.due() is None   # just narrowed: being worked on
    seen["updated_at"] = "2026-01-01 00:00:00"
    assert await p.due() == "1 new point(s) to decide"
    [point] = [x for x in await docket.open_points(db, "researcher") if x["key"] == "source:hn"]
    assert "noisy" in point["title"] and "40 in, 1 behållna" in point["basis"]


async def test_a_mission_sees_the_goals_it_serves_whole_and_the_opportunities_it_found_first(env):
    from agent import opportunities, parts

    db, *_ = env
    items = Items(db)
    jobs = await items.create("aim", "Sommarjobb 2027", "director", kind="goal", owner="director", status="active",
                              data={"why": "Merit", "about": "Hjälper: backend. Brus: banker. Uppdrag: jobs."})
    trip = await items.create("aim", "Utbytet", "director", kind="goal", owner="director", status="active",
                              data={"why": "Ett år ute", "about": "Hjälper: stipendier."})
    text = await parts.goals_for(db, "jobs", "Hitta sommarjobb")
    assert "Brus: banker" in text and "Om: Hjälper: stipendier" not in text and f"- #S{trip} Utbytet" in text
    assert "Hjälper: stipendier" in await parts.goals_for(db, "exchange", f"Bara det som hjälper #S{trip}")   # named by the mission
    other, = [await opportunities.record(db, {"title": "Annat", "url": "https://a.example", "kind": "opportunity"})]
    mine, = [await opportunities.record(db, {"title": "Mitt", "url": "https://m.example", "kind": "opportunity"}, mission="jobs")]
    known = await opportunities.known(db, 1, "jobs")
    assert f"#O{mine[0]}" in known and f"#O{other[0]}" not in known
    assert jobs


async def test_a_test_case_the_search_failed_on_is_a_point_once(env):
    from agent import docket, search_points

    db, *_ = env
    await db.execute("INSERT INTO search_cases (key, origin, title, url, happened, ref, status) VALUES "
                     "('x', 'miss', 'Nybörjarkurs i impro', 'https://impro.example', '2026-11-01', '#J9', 'missed')")
    await search_points._cases(db)
    [p] = [x for x in await docket.open_points(db, "researcher") if x["kind"] == "case"]
    assert "blind fläck" in p["basis"] and "https://impro.example" in p["basis"]
    await docket.answer(db, "researcher", p["id"], "changed", "lade till kurssidan som källa")
    await search_points._cases(db)
    assert not [x for x in await docket.open_points(db, "researcher") if x["kind"] == "case"]   # seen: not again


async def test_a_mission_serving_no_goal_is_a_point_that_clears_when_it_is_disabled(env):
    from agent import docket, search_points

    db, *_ = env
    await search_points._missions(db, NOW)
    assert [x for x in await docket.open_points(db, "researcher") if x["kind"] == "serves"]
    await ops.apply_missions(db, [{"op": "disable", "id": "jobs", "reason": "tjänar inget mål"}])
    await search_points._missions(db, NOW)
    assert not [x for x in await docket.open_points(db, "researcher") if x["kind"] == "serves"]   # done as asked
