"""Where the points to decide come from (agent/docket.py, instance/granskning/2026-11-12-omgang-16-kunskapen.md).

The code raises a point where something happens that a role must take a stand on. Four kinds, all general:

- failing: a watched entry can't be read (raised and resolved by agent/watching.py).
- aging: the owner's own words changed while a role has work that acts on their behalf (missions, watches, rules,
  goals, experiments): a judgement is as old as what it was judged against (omgång 14), and that holds for the
  office's own work too. One open point per role gathers every change until it is answered, with the changed words
  and the role's work beside them, so it can't flood and doesn't hang on words matching (a query for
  "cybersecurity" is not found by a search for "säkerhet").
- lesson: something was closed (an experiment decided, a goal ended, a watch or a rule struck, a request ended).
  What was learned is asked for while it is fresh; the answer becomes a lesson (agent/lessons.py) or "nothing new".
- oversized: a field has grown past its form's size, history kept in what should say where it stands. The role
  writes where it stands; the old text is kept in the item's history.
"""

from __future__ import annotations

import json

from agent import docket
from agent.db import Database
from agent.forms import FORMS, ROLES, oversized
from agent.items import Items, ref

# What acts on the owner's behalf, per role, and so can be overtaken by their words.
ACTING = {
    "researcher": ("routine", "rule", "wanted"),
    "director": ("aim", "rule"),
    "manager": ("routine", "rule", "experiment"),
}
AIM_KINDS = ("goal", "project")      # an aim that acts: a pursuit or an idea is the owner's own to move
CLOSING = ("experiment", "aim", "routine", "rule", "wanted")   # forms whose ending holds a lesson
AGING_CAP = 4000
REVIEWED = ("director", "researcher", "manager")   # the roles whose runs hold the points; another's lesson goes to Director
NEW_OVERSIZED = 5                    # oversized points raised at a time, so a first sweep doesn't bury the rest


# --- aging ----------------------------------------------------------------------------------

def _work_line(item: dict) -> str:
    line = Items.line(item)
    if item["form"] == "routine":
        try:
            mission = json.loads(item.get("config") or "{}").get("mission") or {}
        except ValueError:
            mission = {}
        if qs := mission.get("queries"):
            line += " · frågor: " + " | ".join(str(q)[:60] for q in qs[:10]) + (f" (+{len(qs) - 10})" if len(qs) > 10 else "")
    return line


async def work(db: Database, role: str) -> list[dict]:
    """The role's open work that acts on the owner's behalf."""
    out = []
    for form in ACTING.get(role, ()):
        rows = await Items(db).find(form, open_only=True, owner=role, order="updated_at DESC", limit=60)
        out += [r for r in rows if form != "aim" or r["kind"] in AIM_KINDS]
    return out


async def owner_words_changed(db: Database, item: dict, force: bool = False) -> None:
    """Items watcher: a belief from the owner was made, or changed status (replaced, withdrawn)."""
    if item["form"] != "belief" or (item.get("source") or {}).get("type") != "owner" or item["kind"] == "practice":
        return
    last = await db.fetchone("SELECT status, changes FROM item_log WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item["id"],))
    created = bool(last and last["changes"] and '"created"' in last["changes"])
    if not (force or created or (last and last["status"])):
        return   # an observation added, a field corrected: the words are the same
    for role in ACTING:
        mine = await work(db, role)
        if not mine:
            continue
        open_row = await db.fetchone("SELECT refs FROM docket WHERE role = ? AND key = 'aging' AND status = 'open'", (role,))
        ids = json.loads(open_row["refs"]) if open_row else []
        if ref(item) not in ids:
            ids.append(ref(item))
        beliefs = [b for b in [await Items(db).get(int(i[2:])) for i in ids] if b]
        words = "\n".join(f"- {ref(b)} {b['title']}" + ("" if b["status"] == "current" else f" ({b['status']})") for b in beliefs)
        basis = (f"Ägarens ord som ändrats:\n{words}\n\nDitt arbete som gör något för ägaren (does any of it still search, "
                 "watch, filter or aim for what they no longer want, or miss what they now want? Change what is touched, "
                 "and answer with what you changed, or 'nothing' if nothing is touched):\n" + "\n".join(f"- {_work_line(w)}" for w in mine))
        if len(basis) > AGING_CAP:
            basis = basis[:AGING_CAP].rsplit("\n", 1)[0] + f"\n…(find the rest: find owner={role} open=true)"
        await docket.raise_point(db, role, "aging", "aging", f"Ägarens ord har ändrats ({len(beliefs)}): berörs ditt arbete?",
                                 basis, ids, weight=2, cap=AGING_CAP + 100)


async def seed_aging(db: Database, days: int = 7) -> int:
    """Once, when the points begin: the owner's words of the last days are set beside the work written before them, as
    the shown-once review of omgång 15 let them slide by unanswered."""
    rows = await Items(db).find("belief", where="json_extract(source, '$.type') = 'owner' AND kind != 'practice' "
                                "AND updated_at >= datetime('now', ?)", args=(f"-{days} days",), order="updated_at")
    for b in rows:
        await owner_words_changed(db, b, force=True)
    return len(rows)


# --- lessons --------------------------------------------------------------------------------

LESSON_FIELDS = ("hypothesis", "intervention", "measure", "outcome", "decision", "why", "about", "what", "ends_when", "condition",
                 "reason", "hits", "asked", "interpretation", "post_mortem", "health")


async def closed(db: Database, item: dict) -> None:
    """Items watcher: something ended. Its owner says what was learned."""
    form = FORMS[item["form"]]
    if item["form"] not in CLOSING or item["status"] not in form.closed or item["owner"] not in ROLES:
        return
    role = item["owner"] if item["owner"] in REVIEWED else "director"
    if item["form"] == "aim" and item["kind"] not in (*AIM_KINDS, "idea", "pursuit"):
        return
    last = await db.fetchone("SELECT status, note, changes FROM item_log WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item["id"],))
    if not last or last["status"] != item["status"] or (last["changes"] and '"created"' in last["changes"]):
        return   # not the moment it closed
    facts = [f"{k}: {str(item[k])[:300]}" for k in LESSON_FIELDS if item.get(k) not in (None, "")]
    basis = "\n".join([Items.line(item), *facts] + ([f"Varför det avslutades: {last['note']}"] if last["note"] else []))
    basis += ("\nWhat does this teach about how to work (not about the owner: that is a belief)? Answer with decision "
              "'lesson' and the lesson in one or two sentences, general enough to use next time; or 'nothing' if it "
              "teaches nothing new.")
    await docket.raise_point(db, role, f"lesson:{item['id']}", "lesson", f"Vad lärde vi oss av {ref(item)} {item['title'][:80]}?",
                             basis, [ref(item)], weight=1)


async def resolved_problems(db: Database) -> int:
    """A fault the chat reported and the maintainer marked fixed: what did the office learn from it? Manager's, who owns
    how the office works. Once per fault."""
    rows = await db.fetchall("SELECT id, description, resolved_at FROM problems WHERE resolved_at IS NOT NULL "
                             "AND resolved_at >= datetime('now', '-7 days')")
    n = 0
    for r in rows:
        key = f"lesson:problem:{r['id']}"
        if await db.fetchone("SELECT 1 FROM docket WHERE key = ?", (key,)):
            continue
        await docket.raise_point(
            db, "manager", key, "lesson", f"Vad lärde vi oss av felet som rapporterades (#{r['id']})?",
            f"Rapporterat: {r['description'][:600]}\nMarkerat löst {r['resolved_at'][:10]}.\nIs there something in how the office "
            "works that let it happen, and that a lesson would change? Answer 'lesson' with it, or 'nothing' if it was a "
            "fault in the engine only (that is the maintainer's).", weight=1)
        n += 1
    return n


# --- oversized ------------------------------------------------------------------------------

async def sweep_oversized(db: Database, role: str) -> int:
    """Before a role's run: its open items with fields past their size become points, a few at a time."""
    raised = 0
    have = {r["key"] for r in await db.fetchall("SELECT key FROM docket WHERE role = ? AND kind = 'oversized' AND status = 'open'", (role,))}
    for form in FORMS:
        if raised + len(have) >= NEW_OVERSIZED:
            break
        for item in await Items(db).find(form, open_only=True, owner=role, limit=500):
            over = oversized(item)
            key = f"oversized:{item['id']}"
            if not over or key in have:
                continue
            fields = ", ".join(f"{name} {n} tecken (gräns {limit})" for name, n, limit in over)
            await docket.raise_point(
                db, role, key, "oversized", f"Skriv om {ref(item)} {item['title'][:70]} till hur det står nu",
                f"{fields}.\nRead it, write each field as where things stand now (update), and put what happened on the way "
                "in the note: the old text is kept in the history. Then answer 'changed'.", [ref(item)])
            raised += 1
            if raised + len(have) >= NEW_OVERSIZED:
                break
    return raised


async def resolve_fixed(db: Database, item: dict) -> None:
    """Items watcher: an oversized point clears when its item is within size again, or closed."""
    if oversized(item) and item["status"] not in FORMS[item["form"]].closed:
        return
    await docket.resolve(db, f"oversized:{item['id']}", "inom storleken nu")


WATCHERS = (owner_words_changed, closed, resolve_fixed)
