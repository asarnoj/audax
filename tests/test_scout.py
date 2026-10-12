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
    """Fake Claude: generates numbered queries; research returns one finding per given query."""
    counter = {"n": 0}

    async def runner(task, ws, on_rl):
        calls.append(task)
        if task.kind == "scout_queries":
            n = int(re.search(r"Propose (\d+) new queries", task.prompt).group(1))
            qs = [{"query": f"q{counter['n'] + i}", "rationale": "r"} for i in range(n)]
            counter["n"] += n
            return RunResult(text="", structured={"queries": qs})
        given = [line[2:] for line in task.prompt.splitlines() if line.startswith("- q")]
        return RunResult(text="", structured={"findings": [
            {"title": f"Fynd {q}", "url": f"https://x/{q}", "summary": "s", "why_you": "w", "kind": "opportunity", "query": q} for q in given
        ]})

    return ClaudeGate(db, tmp_path, runner=runner)


async def test_generates_when_portfolio_small_and_selects_batch(db, tmp_path):
    calls = []
    p = Portfolio(db, gate(db, tmp_path, calls), FakeMemory(), load_settings())
    chosen = await p.select(TASK)
    assert len(chosen) == BATCH and calls[0].kind == "scout_queries"
    assert len(await p.stats("jobs")) >= 8


async def test_exploit_good_queries_explore_new_and_retire_dead(db, tmp_path):
    calls = []
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
    r = Researcher(load_settings(), db, gate(db, tmp_path, []), FakeMemory())
    assert await r.run(TASK) == BATCH
    stats = await r.portfolio.stats("jobs")
    assert sum(q["found"] for q in stats) == BATCH


async def test_deep_sweep_uses_wide_batch_and_is_spaced(db, tmp_path):
    from agent.research import DEEP_BATCH

    calls = []
    r = Researcher(load_settings(), db, gate(db, tmp_path, calls), FakeMemory())
    assert await r.run(TASK, deep=True) == DEEP_BATCH
    research = [c for c in calls if c.kind == "research_deep"]
    assert len(research) == 1 and "deep sweep" in research[0].prompt and research[0].max_turns == 120
    assert await r.due_deep() is None  # spaced: no second deep sweep right after
