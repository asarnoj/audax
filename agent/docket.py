"""What a role must decide now (instance/granskning/2026-11-12-omgang-16-kunskapen.md, "Att avgöra nu").

The review used to list what a role should look at, and a line could slide by unanswered run after run (11 Nov: an
entry of the company watch returned 404 and a mission searched for what the owner had ruled out; both were shown, and
neither was answered). A point is different: it is one matter, with what is needed to decide it written in, and it
stays until the role answers it with the `answer` tool. An unanswered point comes back, counted; after a few runs
Manager sees it. A point whose matter clears by itself (the address reads again) is resolved by the code.

The code raises points where something happens: a watched entry starts failing (agent/watching.py), the owner's own
words change while the role has work written before them, something is closed and its lesson is due, a field has
grown past its form's size. One matter is one open point (role + key); raising it again updates its basis.
"""

from __future__ import annotations

import json
from datetime import date

from agent.db import Database

SHOWN_TOO_OFTEN = 3      # shown this many runs without an answer: Manager sees it
BUDGET = 9000            # characters of points in one prompt; the rest waits, counted
BASIS_MAX = 2500         # characters of basis per point
DECISIONS = ("done", "changed", "kept", "struck", "nothing", "later", "lesson")


async def raise_point(db: Database, role: str, key: str, kind: str, title: str, basis: str = "", refs: list[str] | None = None,
                      due: str | None = None, weight: int = 0, cap: int = BASIS_MAX, quiet_days: int = 0) -> int:
    """Opens the point, or updates the open one with the same key. Returns its id, or 0 when the same matter was
    answered within `quiet_days` (a state that persists isn't asked about at every reading)."""
    basis = _cap(basis, cap)
    row = await db.fetchone("SELECT id FROM docket WHERE role = ? AND key = ? AND status = 'open'", (role, key))
    if not row and quiet_days and await db.fetchone(
            "SELECT 1 FROM docket WHERE role = ? AND key = ? AND status = 'answered' AND answered_at >= datetime('now', ?)",
            (role, key, f"-{quiet_days} days")):
        return 0
    if row:
        await db.execute("UPDATE docket SET title = ?, basis = ?, refs = ?, due = ?, weight = max(weight, ?), updated_at = datetime('now') "
                         "WHERE id = ?", (title, basis, json.dumps(refs or []), due, weight, row["id"]))
        return row["id"]
    return await db.insert("docket", role=role, key=key, kind=kind, title=title, basis=basis, refs=json.dumps(refs or []),
                           due=due, weight=weight)


async def resolve(db: Database, key: str, why: str, role: str | None = None) -> int:
    """The matter cleared by itself: the open point is closed, with why."""
    sql, args = "UPDATE docket SET status = 'resolved', answer = ?, answered_at = datetime('now') WHERE key = ? AND status = 'open'", [why, key]
    if role:
        sql += " AND role = ?"; args.append(role)
    return await db.execute_count(sql, tuple(args))


async def get(db: Database, point_id: int) -> dict | None:
    row = await db.fetchone("SELECT * FROM docket WHERE id = ?", (point_id,))
    return dict(row) if row else None


async def open_points(db: Database, role: str, today: date | None = None) -> list[dict]:
    """The role's open points, in the order they are to be decided: the ones with a last day first, then by weight."""
    today = today or date.today()
    rows = await db.fetchall("SELECT * FROM docket WHERE role = ? AND status = 'open' AND (later IS NULL OR later <= ?) "
                             "ORDER BY due IS NULL, due, weight DESC, id", (role, today.isoformat()))
    return [dict(r) for r in rows]


def line(p: dict) -> str:
    head = f"D{p['id']} [{p['kind']}] {p['title']}"
    if p["due"]:
        head += f" · senast {p['due']}"
    if p["shown"]:
        head += f" · obesvarad {p['shown']} gång{'er' if p['shown'] > 1 else ''}"
    return head + ("\n" + "\n".join("  " + x for x in p["basis"].splitlines()) if p["basis"] else "")


async def render(db: Database, role: str, today: date | None = None, budget: int = BUDGET, count: bool = True) -> str:
    """The points for a role's prompt, within the budget. Those shown are counted as shown once more; an answer clears it."""
    points = await open_points(db, role, today)
    if not points:
        return ""
    out, used, shown = [], 0, []
    for p in points:
        text = line(p)
        if out and used + len(text) > budget:
            break
        out.append(text)
        used += len(text)
        shown.append(p["id"])
    rest = len(points) - len(shown)
    if count and shown:
        await db.execute(f"UPDATE docket SET shown = shown + 1 WHERE id IN ({','.join('?' * len(shown))})", tuple(shown))
    head = ("Att avgöra nu (each point needs an answer with the `answer` tool: its id, a decision and a note in Swedish; "
            "decide each on the basis written in, and read more only if you need to. An unanswered point comes back):")
    tail = f"\n({rest} punkt(er) till väntar; de kommer i nästa körning)" if rest else ""
    return head + "\n" + "\n\n".join(out) + tail


async def answer(db: Database, role: str, point_id: int, decision: str, note: str, later: str | None = None) -> dict:
    """The role's answer. 'later' needs a day; the point comes back then. Returns the point."""
    p = await get(db, point_id)
    if p is None or p["role"] != role:
        raise ValueError(f"D{point_id} is not one of your points")
    if p["status"] != "open":
        raise ValueError(f"D{point_id} is already {p['status']}")
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    if not (note or "").strip():
        raise ValueError("a note is needed: what you decided and why, in Swedish")
    if decision == "later":
        try:
            day = date.fromisoformat((later or "")[:10])
        except ValueError:
            raise ValueError("'later' needs the day it comes back (YYYY-MM-DD)") from None
        await db.execute("UPDATE docket SET later = ?, answer = ?, shown = 0, updated_at = datetime('now') WHERE id = ?",
                         (day.isoformat(), note.strip(), point_id))
    else:
        await db.execute("UPDATE docket SET status = 'answered', decision = ?, answer = ?, answered_at = datetime('now'), "
                         "updated_at = datetime('now') WHERE id = ?", (decision, note.strip(), point_id))
    return await get(db, point_id)


async def unanswered(db: Database) -> str:
    """For Manager: points other roles have been shown again and again without answering."""
    rows = await db.fetchall("SELECT * FROM docket WHERE status = 'open' AND shown >= ? ORDER BY shown DESC, id", (SHOWN_TOO_OFTEN,))
    if not rows:
        return ""
    return ("Punkter som lämnats obesvarade (the office's own matters sliding by: ask the role with a post, or tell the "
            "maintainer if a point can't be answered):\n"
            + "\n".join(f"- D{r['id']} hos {r['role']}: {r['title'][:120]} · visad {r['shown']} gånger" for r in rows))


async def new_since(db: Database, role: str, since_utc: str) -> int:
    r = await db.fetchone("SELECT count(*) AS n FROM docket WHERE role = ? AND status = 'open' AND created_at > ?", (role, since_utc))
    return r["n"] if r else 0


async def stats(db: Database, days: int = 14) -> dict[str, dict[str, int]]:
    """Per role: raised, answered, resolved by themselves and still open, over the last days."""
    out: dict[str, dict[str, int]] = {}
    for r in await db.fetchall("SELECT role, status, count(*) AS n FROM docket WHERE created_at >= datetime('now', ?) GROUP BY role, status",
                               (f"-{days} days",)):
        out.setdefault(r["role"], {})[r["status"]] = r["n"]
    return out


def _cap(text: str, cap: int = BASIS_MAX) -> str:
    text = (text or "").strip()
    return text if len(text) <= cap else text[:cap].rsplit("\n", 1)[0] + "\n…(read the items for the rest)"

