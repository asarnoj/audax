import json
import re

import pytest

from agent.claude_gate import ClaudeGate, RunResult
from agent.config import load_settings
from agent.db import Database
from agent.research import BATCH, EXPLORE, RETIRE_AFTER_RUNS, Portfolio, ResearchTask, Researcher

TASK = ResearchTask("jobs", "Hitta praktik", "Stockholm", 3, True)


class FakeMemory:
    path = __import__("pathlib").Path("/nonexistent-memory")

    def read(self, *names):
        return ""


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


def gate(db, tmp_path, calls):
    """Fake Claude: research returns one finding per given query."""

    async def runner(task, ws, on_rl):
        calls.append(task)
        given = [line[2:] for line in task.prompt.splitlines() if line.startswith("- q")]
        return RunResult(text="", structured={"findings": [
            {"title": f"Fynd {q}", "url": f"https://x/{q}", "summary": "s", "why_you": "w", "kind": "opportunity", "query": q} for q in given
        ], "leads": [], "skipped": []})

    return ClaudeGate(db, tmp_path, runner=runner)


async def seed(db, n=12, mission="jobs"):
    """Queries come from Researcher's planning (agent/research_plan.py); here they are seeded."""
    for i in range(n):
        await db.execute("INSERT INTO scout_queries (mission, query, rationale) VALUES (?, ?, 'r')", (mission, f"q{i}"))


async def test_selects_a_batch_from_the_portfolio_without_generating(db, tmp_path):
    calls = []
    await seed(db)
    p = Portfolio(db, gate(db, tmp_path, calls), FakeMemory(), load_settings())
    chosen = await p.select(TASK)
    assert len(chosen) == BATCH and calls == []  # no separate query generator any more


async def test_exploit_good_queries_explore_new_and_retire_dead(db, tmp_path):
    calls = []
    await seed(db)
    g = gate(db, tmp_path, calls)
    r = Researcher(load_settings(), db, g, FakeMemory())
    await r.run(TASK)
    first = [q["query"] for q in await r.portfolio.stats("jobs") if q["runs"] == 1]
    assert len(first) == BATCH

    # The owner kept the finding from the first query only (it was sent); everything else was dropped.
    winner = first[0]
    for e in await db.fetchall("SELECT id, payload FROM events"):
        q = json.loads(e["payload"])["query"]
        await db.execute("INSERT INTO candidates (event_id, kind, title, body, status) VALUES (?, 'opportunity', 't', 'b', ?)",
                         (e["id"], "sent" if q == winner else "dropped"))

    chosen = await r.portfolio.select(TASK)
    assert winner in chosen  # exploit
    assert sum(1 for q in chosen if q not in first) >= EXPLORE  # explore

    for _ in range(RETIRE_AFTER_RUNS):
        await r.portfolio.record_run("jobs", first)
    await r.portfolio.select(TASK)
    status = {q["query"]: q["status"] for q in await r.portfolio.stats("jobs")}
    assert status[winner] == "active"
    assert all(status[q] == "retired" for q in first[1:])  # repeatedly fruitless -> retired


async def test_findings_are_attributed_to_their_query(db, tmp_path):
    await seed(db)
    r = Researcher(load_settings(), db, gate(db, tmp_path, []), FakeMemory())
    assert await r.run(TASK) == BATCH
    stats = await r.portfolio.stats("jobs")
    assert sum(q["found"] for q in stats) == BATCH


async def test_a_deep_sweep_runs_only_when_researcher_asked_and_is_spaced(db, tmp_path, monkeypatch):
    from agent import research
    from agent.research import DEEP_BATCH

    monkeypatch.setattr(research, "load_research", lambda: [TASK])
    calls = []
    await seed(db, 30)
    r = Researcher(load_settings(), db, gate(db, tmp_path, calls), FakeMemory())
    assert await r.due_deep() is None  # nobody asked
    await db.kv_set("research.deep_requested.jobs", "2026-10-13T05:45:00+02:00")
    assert (await r.due_deep()).id == "jobs"
    assert await r.run(TASK, deep=True) == DEEP_BATCH
    research_calls = [c for c in calls if c.kind == "research_deep"]
    assert len(research_calls) == 1 and "deep sweep" in research_calls[0].prompt and research_calls[0].max_turns == 120
    assert await db.kv_get("research.deep_requested.jobs") is None  # done
    await db.kv_set("research.deep_requested.jobs", "2026-10-13T06:00:00+02:00")
    assert await r.due_deep() is None  # spaced: no second deep sweep right after
