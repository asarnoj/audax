import pytest

from agent import thoughts
from agent.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


async def test_a_thought_joins_the_outbox_and_its_role_sees_how_it_landed(db):
    made = await thoughts.add(db, "researcher", [
        {"kind": "working_on", "title": "Praktiken", "thought": "De flesta öppnar i november.", "why_them": "så du vet",
         "question": "Vill du att jag letar utomlands också?"},
        {"kind": "idea", "title": "Tom", "thought": " ", "why_them": "x"}], "Europe/Stockholm")
    assert made == ["tanke till ägaren: Praktiken"]   # an empty one is nothing
    c = await db.fetchone("SELECT id, kind, status, body FROM candidates")
    assert (c["kind"], c["status"]) == ("thought", "pending") and "Fråga att ställa rakt ut" in c["body"]
    m = await db.execute("INSERT INTO messages (telegram_message_id, direction, origin, text) VALUES (1, 'out', 'proactive', 't')")
    await db.execute("INSERT INTO message_candidates (message_id, candidate_id) VALUES (?, ?)", (m, c["id"]))
    await db.execute("UPDATE candidates SET status = 'sent' WHERE id = ?", (c["id"],))
    await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (2, 'in', 'ja gärna')")
    text = await thoughts.outcomes(db, "researcher")
    assert "Praktiken: sent, besvarad" in text and await thoughts.outcomes(db, "manager") == ""
