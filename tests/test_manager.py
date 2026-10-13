import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.claude_gate import ClaudeGate, RunResult
from agent.config import load_settings
from agent.db import Database
from agent.manager import CHAT_SEED_KEY, Manager
from agent.memory import MemoryRepo

NOW = datetime(2026, 10, 12, 3, 30, tzinfo=ZoneInfo("Europe/Stockholm"))


@pytest.fixture
async def env(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "policy.md").write_text("# Policy\nkallstart\n")
    (mem / "changelog.md").write_text("# Ändringslogg\n")
    subprocess.run(["git", "init", "-q"], cwd=mem, check=True)
    research = None

    db = Database(tmp_path / "t.db")
    await db.open()
    yield db, MemoryRepo(mem), research
    await db.close()


def manager(db, memory, tmp_path, structured=None):
    async def runner(task, ws, on_rl):
        return RunResult(text="", structured=structured)

    return Manager(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), memory)


BASE = {"policy_md": None, "policy_reason": "", "journal_md": "Lugn dag.", "changelog": "", "checkin": False, "restart_chat": False, "chat_summary": "Sammanfattning."}


async def test_apply_writes_policy_journal_changelog_and_commits(env, tmp_path):
    db, memory, _ = env
    m = manager(db, memory, tmp_path)
    await m.apply({**BASE, "policy_md": "# Policy\nnytt läge", "policy_reason": "mer feedback", "changelog": "Justerade tempo."}, NOW)

    assert (memory.path / "policy.md").read_text() == "# Policy\nnytt läge\n"
    assert "Lugn dag." in (memory.path / "journal" / "2026-10-12.md").read_text()
    log = (memory.path / "changelog.md").read_text()
    assert "Justerade tempo." in log and "policy: mer feedback" in log
    assert await memory.changed_files() == []  # committed


async def test_one_active_experiment_per_area(env, tmp_path):
    db, memory, _ = env
    m = manager(db, memory, tmp_path)
    exp = lambda h: {"area": "timing", "hypothesis": h, "evaluate_after": "2026-10-19"}  # noqa: E731
    await m.apply({**BASE, "experiments_new": [exp("A"), exp("B")]}, NOW)
    rows = await db.fetchall("SELECT hypothesis FROM experiments WHERE status = 'active'")
    assert [r["hypothesis"] for r in rows] == ["A"]

    await m.apply({**BASE, "experiments_close": [{"id": 1, "status": "kept", "outcome": "funkade"}], "experiments_new": [exp("C")]}, NOW)
    rows = await db.fetchall("SELECT hypothesis, status FROM experiments ORDER BY id")
    assert [(r["hypothesis"], r["status"]) for r in rows] == [("A", "kept"), ("C", "active")]


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


async def test_note_goes_through_outbox_and_restart_seeds_chat(env, tmp_path):
    db, memory, _ = env
    await db.kv_set("chat.session_id", "old")
    m = manager(db, memory, tmp_path)
    await m.apply({**BASE, "note_to_owner": "Jag skickar lite färre jobbtips nu.", "restart_chat": True, "chat_summary": "Pratade om Google."}, NOW)
    row = await db.fetchone("SELECT kind, status, body FROM candidates")
    assert (row["kind"], row["status"]) == ("manager_note", "pending")  # the dispatcher decides when
    assert await db.kv_get("chat.session_id") is None
    assert await db.kv_get(CHAT_SEED_KEY) == "Pratade om Google."


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
    m = manager(db, memory, tmp_path, structured={**BASE, "changelog": "Inget att ändra än, för lite data."})
    out = await m.run()
    assert out["changelog"].startswith("Inget att ändra")
    assert await db.kv_get("manager.last_run") is not None


async def test_manager_v2_style_state_shelf_and_questions(env, tmp_path):
    db, memory, _ = env
    (memory.path / "style.md").write_text("# Stil\nkort\n")
    shelved = await db.execute("INSERT INTO candidates (kind, title, body, status) VALUES ('event', 'Hack', 'b', 'shelved')")
    sent = await db.execute("INSERT INTO candidates (kind, title, body, status) VALUES ('event', 'X', 'b', 'sent')")
    from agent.posts import Posts

    note = await Posts(db).send("assistant", "manager", "note", "längd", verbatim="det där var för långt", from_owner=True)
    m = manager(db, memory, tmp_path)
    m._seen_posts = [note]
    await m.apply({**BASE, "working_memory_md": "Fokus: längden.", "posts": [{"to": "director", "kind": "note", "body": "Julius verkar stressad"}], "style_md": "# Stil\nännu kortare", "style_reason": "två klagomål på längd",
                   "state_md": "Tentavecka.", "resurface": [shelved, sent],
                   "questions": [{"what": "Funkar kvällarna?", "why": "timing"}, {"what": "extra", "why": "x"}]}, NOW)

    assert (memory.path / "style.md").read_text() == "# Stil\nännu kortare\n"
    assert (memory.path / "state.md").read_text() == "Tentavecka.\n"
    st = {r["id"]: r["status"] for r in await db.fetchall("SELECT id, status FROM candidates")}
    assert st == {shelved: "pending", sent: "sent"}  # only shelved items can be resurfaced
    qs = await db.fetchall("SELECT sender, recipient, body FROM posts WHERE kind = 'question'")
    assert [(r["sender"], r["recipient"], r["body"]) for r in qs] == [("manager", "assistant", "Funkar kvällarna?")]  # at most one
    assert await Posts(db).inbox("manager") == []  # read, so handled
    assert [p["body"] for p in await Posts(db).inbox("director")] == ["Julius verkar stressad"]
    assert (memory.path / "arbetsminne" / "manager.md").read_text() == "Fokus: längden.\n"
    assert "stilguide: två klagomål på längd" in (memory.path / "changelog.md").read_text()



async def test_big_chat_session_is_restarted_from_the_summary(env, tmp_path):
    db, memory, _ = env
    await db.kv_set("chat.session_id", "big")
    await db.execute("INSERT INTO claude_runs (kind, is_error, context_tokens) VALUES ('chat', 0, 109000)")
    await manager(db, memory, tmp_path).apply({**BASE, "chat_summary": "Pratade om tentan."}, NOW)
    assert await db.kv_get("chat.session_id") is None and await db.kv_get(CHAT_SEED_KEY) == "Pratade om tentan."


async def test_small_chat_session_is_kept(env, tmp_path):
    db, memory, _ = env
    await db.kv_set("chat.session_id", "small")
    await db.execute("INSERT INTO claude_runs (kind, is_error, context_tokens) VALUES ('chat', 0, 30000)")
    await manager(db, memory, tmp_path).apply(BASE, NOW)
    assert await db.kv_get("chat.session_id") == "small"


async def test_manager_owns_the_schedule_but_not_the_owners_bookings(env, tmp_path):
    from agent.agenda import Agenda
    from agent.config import load_settings
    from agent.schedule import Schedule

    db, memory, _ = env

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
    assert "bara ägaren kan stryka" in (memory.path / "changelog.md").read_text()
