import json
from datetime import date, timedelta

import pytest

from agent.db import Database
from agent.pipeline.outbox import add_candidate
from agent.pursuits import Pursuits, candidates_of_message


class FixedDay(Pursuits):
    day = date(2026, 10, 20)

    def today(self):
        return self.day


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


async def test_upsert_matches_by_title_and_logs_status(db):
    p = Pursuits(db)
    pid, created = await p.upsert({"title": "Google SRE Intern 2027", "organization": "Google", "deadline": "2026-10-23"}, "chat")
    assert created
    same, created = await p.upsert({"title": "google sre intern 2027", "status": "applied", "note": "sökte i kväll"}, "chat")
    assert (same, created) == (pid, False)
    row = await db.fetchone("SELECT status, deadline FROM pursuits WHERE id = ?", (pid,))
    assert (row["status"], row["deadline"]) == ("applied", "2026-10-23")
    assert [r["status"] for r in await db.fetchall("SELECT status FROM pursuit_log ORDER BY id")] == ["interested", "applied"]


async def test_a_delivered_tip_becomes_a_pursuit_with_its_deadline(db):
    eid = await db.execute("INSERT INTO events (source, kind, dedupe_key, payload) VALUES ('research:jobs', 'opportunity', 'k', ?)",
                           (json.dumps({"title": "Google SRE", "organization": "Google", "deadline": "2026-10-23"}),))
    cid = await add_candidate(db, "opportunity", "Google SRE", "b", {}, url="https://g", event_id=eid)
    mid = await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (5, 'out', 'tips')")
    await db.execute("INSERT INTO message_candidates (message_id, candidate_id) VALUES (?, ?)", (mid, cid))
    assert await candidates_of_message(db, mid) == [cid]  # chat-written deliveries link here, not in messages.candidate_id
    pid = await Pursuits(db).from_candidate(cid, "reaction")
    row = await db.fetchone("SELECT * FROM pursuits WHERE id = ?", (pid,))
    assert (row["organization"], row["deadline"], row["url"], row["source"]) == ("Google", "2026-10-23", "https://g", "reaction")
    assert await Pursuits(db).from_candidate(cid, "reaction") == pid  # once


async def test_one_nudge_before_the_deadline_and_missed_after_it(db):
    p = FixedDay(db)
    soon, _ = await p.upsert({"title": "Ideate", "deadline": "2026-10-22"}, "chat")  # 2 days left: due
    later, _ = await p.upsert({"title": "Spotify", "deadline": "2026-11-30"}, "chat")  # not yet
    asked, _ = await p.upsert({"title": "Armada", "remind_at": "2026-10-20"}, "chat")  # owner asked for today
    done, _ = await p.upsert({"title": "KTH utbyte", "deadline": "2026-10-21", "status": "applied"}, "chat")  # applied: no nudge
    past, _ = await p.upsert({"title": "Hackathon", "deadline": "2026-10-19"}, "chat")
    assert await p.tick() == 2
    nudges = await db.fetchall("SELECT title, body, features FROM candidates WHERE kind = 'nudge' ORDER BY id")
    assert [n["title"] for n in nudges] == ["Ideate", "Armada"]
    assert json.loads(nudges[0]["features"])["time_sensitivity"] == "high" and "om 2 dag" in nudges[0]["body"]
    assert "bad om påminnelsen" in nudges[1]["body"]
    assert await p.tick() == 0  # one nudge per pursuit
    assert (await db.fetchone("SELECT status FROM pursuits WHERE id = ?", (past,)))["status"] == "missed"

    p.day = date(2026, 11, 27)
    assert await p.tick() == 1  # Spotify's turn


async def test_stats_show_whether_a_nudge_was_followed(db):
    p = FixedDay(db)
    pid, _ = await p.upsert({"title": "Ideate", "deadline": "2026-10-22"}, "chat")
    await p.tick()
    await db.execute("UPDATE pursuits SET nudged_at = datetime('now', '-1 minute') WHERE id = ?", (pid,))
    await p.upsert({"id": pid, "status": "applied"}, "chat")
    text = await p.stats()
    assert "Nudges: 1, followed by preparing or applying: 1" in text and "sökt 1" in text


async def test_goals_hold_what_serves_them_and_only_opportunities_are_nudged(db):
    p = FixedDay(db)
    goal, _ = await p.upsert({"kind": "goal", "title": "Sommarjobb 2027", "why": "Komma in i branschen", "about": "Hjälper: scaleups\nBrus: banker",
                              "next_step": "Armada", "deadline": "2026-10-21"}, "director")
    google, _ = await p.upsert({"title": "Google SRE", "organization": "Google", "deadline": "2026-10-23", "parent": goal}, "chat")
    idea, _ = await p.upsert({"kind": "idea", "title": "Mässor är en färdig ram", "status": "dismissed", "parent": goal}, "director")
    await p.upsert({"kind": "project", "title": "Spel med storebror", "parent": google}, "chat")   # only a goal or project holds others
    assert (await db.fetchone("SELECT parent_id FROM pursuits WHERE kind = 'project'"))["parent_id"] is None
    assert (await db.fetchone("SELECT status FROM pursuits WHERE id = ?", (goal,)))["status"] == "active"   # each kind its own statuses
    same, created = await p.upsert({"kind": "goal", "title": "Google SRE"}, "director")   # titles match within a kind
    assert created

    full = await p.overview()
    assert full.index("#S1 Mål: Sommarjobb 2027") < full.index("  - #S2 Tillfälle: Google SRE (Google)")
    assert "Varför: Komma in i branschen" in full and "Brus: banker" in full and "Spel med storebror" in full
    assert full.index("Avslutat") < full.index("#S3 Idé: Mässor är en färdig ram · avfärdad")   # closed, but kept in view
    brief = await p.overview(notes=False, ideas=False)
    assert "Brus" not in brief and "nästa: Armada" in brief and "Idé" not in brief

    p.day = date(2026, 10, 22)
    assert await p.tick() == 1   # the opportunity is nudged; the goal's own deadline is not
    assert (await db.fetchone("SELECT status FROM pursuits WHERE id = ?", (goal,)))["status"] == "active"
    assert idea and google
