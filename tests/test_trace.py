"""Following one act back to why (agent/trace.py)."""

import json

import pytest

from agent import docket, trail
from agent.db import Database
from agent.items import Items
from agent.trace import Trace


@pytest.fixture
async def db(tmp_path, monkeypatch):
    d = Database(tmp_path / "t.db")
    await d.open()
    monkeypatch.setattr(Items, "watchers", [])
    yield d
    await d.close()


async def run(db, kind, prompt, steps, output=None):
    return await db.insert("claude_runs", kind=kind, prompt=prompt, transcript=json.dumps(steps), output=json.dumps(output or {}))


async def test_an_item_is_followed_to_the_run_that_changed_it_and_what_that_run_was_shown(db):
    items = Items(db)
    belief = await items.create("belief", "Får störas utom under föreläsningar", "assistant", kind="preference",
                                source={"type": "owner", "quote": "utom föreläsningar"})
    rid = await run(db, "director_understand", f"# Mål\nSpelet: se #U{belief}\n# Annat\nx",
                    [{"tool": "office__update", "input": '{"id": "#S9"}'}, {"result": "ok", "error": False}])
    token = trail.current_run.set(rid)
    try:
        gid = await items.create("aim", "Spel med brodern", "director", kind="project", data={"next_step": f"Bygga i egen takt (#U{belief})"})
    finally:
        trail.current_run.reset(token)
    text = await Trace(db)(f"S{gid}")
    assert f"#R{rid} director_understand" in text            # who changed it, in which run
    assert f"#U{belief}" in text and "källa owner" in text   # what it names, and where that came from
    assert f"| Spelet: se #U{belief}" not in text            # the run is searched for this item, not for what it names
    assert "update: " in text                                # what the run did


async def test_a_finding_is_followed_through_its_decisions_to_the_message(db):
    cid = await db.insert("candidates", kind="event", title="Hackathon", body="", status="sent",
                          features=json.dumps({"relevance": "medium", "reason": "online"}))
    tid = await run(db, "triage", f"[event_id 7] Hackathon\n...", [])
    await db.insert("decisions", candidate_id=cid, module="triage@1", tier="keep", reason="online och AI", run_id=tid)
    did = await run(db, "dispatcher", f"Fynd #F{cid}: Hackathon", [], {"decisions": [{"candidate_id": cid}]})
    await db.insert("decisions", candidate_id=cid, module="dispatcher@2", tier="planerad", reason="nattro nu", run_id=did)
    mid = await db.insert("messages", telegram_message_id=1, direction="out", text="Ett hackathon online", candidate_id=cid)
    text = await Trace(db)(f"M{mid}")
    assert "triage@1: keep" in text and "dispatcher@2: planerad" in text and "nattro nu" in text
    assert f"| Fynd #F{cid}: Hackathon" in text   # the desk's prompt, at the line about this finding


async def test_a_point_shows_its_basis_and_the_run_that_answered_it(db):
    pid = await docket.raise_point(db, "manager", "cite:policy.md", "cite", "Säger raderna det de hänvisar till?", "- en rad\n  → #U1")
    await run(db, "manager", "Att avgöra nu\nD%d ..." % pid, [{"tool": "office__answer", "input": json.dumps({"id": f"D{pid}"})}])
    text = await Trace(db)(f"D{pid}")
    assert "Säger raderna" in text and "answer:" in text
    assert "Förstår inte" in await Trace(db)("xyz")
