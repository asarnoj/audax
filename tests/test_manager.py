import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent import trail
from agent.claude_gate import ClaudeGate, RunResult
from agent.config import load_settings
from agent.db import Database
from agent.manager import Manager
from agent.memory import MemoryRepo

NOW = datetime(2026, 10, 12, 3, 30, tzinfo=ZoneInfo("Europe/Stockholm"))


@pytest.fixture
async def env(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "policy.md").write_text("# Policy\nkallstart\n")
    subprocess.run(["git", "init", "-q"], cwd=mem, check=True)
    research = None

    db = Database(tmp_path / "t.db")
    await db.open()
    trail.attach(db)
    yield db, MemoryRepo(mem), research
    trail.attach(None)
    trail.current_run.set(None)
    await db.close()


async def begin(db) -> None:
    """A run in progress, so Manager's account has somewhere to go (as when the gate runs it)."""
    trail.current_run.set(await db.execute("INSERT INTO claude_runs (kind) VALUES ('manager')"))


async def account(db) -> str:
    return (await db.fetchone("SELECT summary FROM claude_runs ORDER BY id DESC LIMIT 1"))["summary"] or ""


def manager(db, memory, tmp_path, structured=None):
    async def runner(task, ws, on_rl):
        return RunResult(text="", structured=structured)

    return Manager(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), memory)


BASE = {"policy_md": None, "policy_reason": "", "summary": "", "checkin": False,
        "desk_review": {"sampled": 3, "followed_policy": True, "notes": ""}}


async def test_apply_writes_policy_keeps_its_account_on_the_run_and_commits(env, tmp_path):
    db, memory, _ = env
    await begin(db)
    m = manager(db, memory, tmp_path)
    await m.apply({**BASE, "policy_md": "# Policy\nnytt läge", "policy_reason": "mer feedback", "summary": "Justerade tempo."}, NOW)

    assert (memory.path / "policy.md").read_text() == "# Policy\nnytt läge\n"
    log = await account(db)
    assert log.startswith("Justerade tempo.") and "policy: mer feedback" in log
    assert not (memory.path / "journal").exists() and not (memory.path / "changelog.md").exists()
    assert await memory.changed_files() == []  # committed


async def test_proposal_flow_one_at_a_time(env, tmp_path):
    db, memory, _ = env
    m = manager(db, memory, tmp_path)
    proposal = {"kind": "other", "title": "Tystare kvällar", "description": "Inga utskick efter 21 på vardagar?"}
    await m.apply({**BASE, "proposal": proposal}, NOW)
    await m.apply({**BASE, "proposal": {**proposal, "title": "Ett till"}}, NOW)  # only one open proposal at a time
    assert (await db.fetchone("SELECT count(*) AS n FROM proposals"))["n"] == 1

    q = await m.next_question()
    assert q["type"] == "proposal" and "Tystare kvällar" in q["text"]
    await m.mark_asked(q, None)
    assert await m.next_question() is None  # waiting for the answer
    assert "tar med det" in await m.decide_proposal(q["id"], True)
    assert "redan avgjort" in await m.decide_proposal(q["id"], False)


async def test_note_goes_through_the_desk_and_the_spot_check_is_logged(env, tmp_path):
    db, memory, _ = env
    await begin(db)
    m = manager(db, memory, tmp_path)
    await m.apply({**BASE, "note_to_owner": "Jag skickar lite färre jobbtips nu.",
                   "desk_review": {"sampled": 5, "followed_policy": False, "notes": "skickade sent på kvällen trots policyn"}}, NOW)
    row = await db.fetchone("SELECT kind, status, body FROM candidates")
    assert (row["kind"], row["status"]) == ("manager_note", "pending")  # the dispatcher decides when
    assert "gled från policyn: skickade sent på kvällen" in await account(db)


async def test_checkin_is_queued_as_question(env, tmp_path):
    db, memory, _ = env
    m = manager(db, memory, tmp_path)
    await m.apply({**BASE, "checkin": True}, NOW)
    q = await m.next_question()
    assert q["type"] == "checkin" and "Lagom" in q["options"]
    await m.mark_asked(q, None)
    assert await m.next_question() is None


async def test_run_end_to_end_with_fake_claude(env, tmp_path):
    db, memory, _ = env
    m = manager(db, memory, tmp_path, structured={**BASE, "summary": "Inget att ändra än, för lite data."})
    out = await m.run()
    assert out["summary"].startswith("Inget att ändra") and (await account(db)).startswith("Inget att ändra")
    assert await db.kv_get("manager.last_run") is not None


async def test_manager_shelf_posts_and_questions(env, tmp_path):
    db, memory, _ = env
    shelved = await db.execute("INSERT INTO candidates (kind, title, body, status) VALUES ('event', 'Hack', 'b', 'shelved')")
    sent = await db.execute("INSERT INTO candidates (kind, title, body, status) VALUES ('event', 'X', 'b', 'sent')")
    from agent.posts import Posts

    note = await Posts(db).send("assistant", "manager", "note", "längd", verbatim="det där var för långt", from_owner=True)
    m = manager(db, memory, tmp_path)
    m._seen_posts = [note]
    await m.apply({**BASE, "working_memory_md": "Fokus: längden.", "posts": [{"to": "director", "kind": "note", "body": "Julius verkar stressad"}], "resurface": [shelved, sent]}, NOW)

    st = {r["id"]: r["status"] for r in await db.fetchall("SELECT id, status FROM candidates")}
    assert st == {shelved: "pending", sent: "sent"}  # only shelved items can be resurfaced
    assert await Posts(db).inbox("manager") == []  # read, so handled
    assert [p["body"] for p in await Posts(db).inbox("director")] == ["Julius verkar stressad"]
    assert (memory.path / "arbetsminne" / "manager.md").read_text() == "Fokus: längden.\n"



async def test_manager_owns_the_schedule_but_not_the_owners_bookings(env, tmp_path):
    from agent.agenda import Agenda
    from agent.config import load_settings
    from agent.schedule import Schedule

    db, memory, _ = env
    await begin(db)

    class Clock(Schedule):
        def now(self):
            return NOW

        async def slots(self, start, end, include_not_mine=False):
            return []

    agenda = Agenda(db, Clock(load_settings(), db, []))
    planned = await db.execute("INSERT INTO candidates (kind, title, body, status, deliver_at) VALUES ('event', 'Hack', 'b', 'scheduled', '2026-10-12T10:00:00+00:00')")
    own, _ = await agenda.book("reminder", "Google", "Sök Google", NOW.replace(day=12, hour=9), "assistant", fixed=True)
    m = manager(db, memory, tmp_path)
    m.agenda = agenda
    changes = []
    await m.apply({**BASE, "agenda_changes": [
        {"id": f"#F{planned}", "action": "move", "at": "2026-10-13 09:00", "reason": "för tätt i dag"},
        {"id": f"#B{own}", "action": "cancel", "reason": "onödig"},
    ]}, NOW)
    assert (await db.fetchone("SELECT deliver_at FROM candidates WHERE id = ?", (planned,)))["deliver_at"].startswith("2026-10-13T07:00")
    assert (await db.fetchone("SELECT status FROM bookings WHERE id = ?", (own,)))["status"] == "planned"  # the owner's own: fixed
    assert "bara ägaren kan stryka" in await account(db)
