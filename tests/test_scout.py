import json
import re

import pytest

from agent.claude_gate import ClaudeGate, RunResult
from agent.config import load_settings
from agent.db import Database
from agent.research import BATCH, EXPLORE, RETIRE_AFTER_RUNS, Portfolio, ResearchTask, Researcher

TASK = ResearchTask("jobs", "Hitta praktik", "Stockholm", 3, True, tuple(f"q{i}" for i in range(12)))


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


async def test_selects_a_batch_from_the_portfolio_without_generating(db, tmp_path):
    calls = []
    p = Portfolio(db, gate(db, tmp_path, calls), FakeMemory(), load_settings())
    chosen = await p.select(TASK)
    assert len(chosen) == BATCH and calls == []  # no separate query generator any more


async def test_exploit_good_queries_explore_new_and_flag_dead(db, tmp_path, monkeypatch):
    from agent import research

    calls = []
    g = gate(db, tmp_path, calls)
    r = Researcher(load_settings(), db, g, FakeMemory())
    await r.run(TASK)
    first = [q["query"] for q in await r.portfolio.stats("jobs", TASK.queries) if q["runs"] == 1]
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
    monkeypatch.setattr(research, "load_research", lambda: [TASK])
    stale = await r.portfolio.stale("jobs")
    assert winner not in stale and set(first[1:]) <= set(stale)  # repeatedly fruitless: Researcher is told to replace them


async def test_findings_are_attributed_to_their_query(db, tmp_path):
    r = Researcher(load_settings(), db, gate(db, tmp_path, []), FakeMemory())
    assert await r.run(TASK) == BATCH
    stats = await r.portfolio.stats("jobs", TASK.queries)
    assert sum(q["found"] for q in stats) == BATCH


async def test_a_deep_sweep_runs_only_when_researcher_asked_and_is_spaced(db, tmp_path, monkeypatch):
    from agent import research
    from agent.research import DEEP_BATCH

    deep = TASK.__class__(**{**TASK.__dict__, "queries": tuple(f"q{i}" for i in range(30))})
    monkeypatch.setattr(research, "load_research", lambda: [deep])
    calls = []
    r = Researcher(load_settings(), db, gate(db, tmp_path, calls), FakeMemory())
    assert await r.due_deep() is None  # nobody asked
    await db.kv_set("research.deep_requested.jobs", "2026-10-13T05:45:00+02:00")
    assert (await r.due_deep()).id == "jobs"
    assert await r.run(deep, deep=True) == DEEP_BATCH
    research_calls = [c for c in calls if c.kind == "research_deep"]
    assert len(research_calls) == 1 and "deep sweep" in research_calls[0].prompt and research_calls[0].max_turns == 120
    assert await db.kv_get("research.deep_requested.jobs") is None  # done
    await db.kv_set("research.deep_requested.jobs", "2026-10-13T06:00:00+02:00")
    assert await r.due_deep() is None  # spaced: no second deep sweep right after


def test_a_finding_whose_page_was_never_read_says_so():
    from agent.research import hold_to_the_page

    london = "https://www.google.com/about/careers/applications/jobs/results/100028133205254854-intern-2027"
    f = {"url": london, "checked": "page", "unconfirmed": None}
    hold_to_the_page(f, {"https://www.google.com/about/careers/applications/jobs/results/?q=intern"})
    assert f["checked"] == "other_sites" and "inte bekräftade" in f["unconfirmed"]
    ok = {"url": london + "/", "checked": "page", "unconfirmed": None}
    hold_to_the_page(ok, {london})
    assert ok == {"url": london + "/", "checked": "page", "unconfirmed": None}
