"""Where the points to decide come from (agent/docket.py, instance/granskning/2026-11-12-omgang-16-kunskapen.md).

The code raises a point where something happens that a role must take a stand on. Four kinds, all general:

- failing: a watched entry can't be read (raised and resolved by agent/watching.py).
- aging: the owner's own words changed while a role has work that acts on their behalf (missions, watches, rules,
  goals, experiments) or its own interpretations of them in the same part of their life: a judgement is as old as
  what it was judged against (omgång 14), and that holds for the office's own work and conclusions too. One open
  point per role gathers every change until it is answered, with the changed words beside the work and the
  interpretations, so it can't flood and doesn't hang on words matching (a query for "cybersecurity" is not found by
  a search for "säkerhet"; an inferred interest in security stood a week after the owner said it wasn't theirs).
- stale: an open item's next step names something that has closed, or a day that has passed (a goal's next step
  still read "shared before 1 Nov, see #E133" two weeks after the experiment was dropped, in six kinds of prompt).
- lesson: something was closed (an experiment decided, a goal ended, a watch or a rule struck, a request ended).
  What was learned is asked for while it is fresh; the answer becomes a lesson (agent/lessons.py) or "nothing new".
- oversized: a field has grown past its form's size, history kept in what should say where it stands. The role
  writes where it stands; the old text is kept in the item's history.
"""

from __future__ import annotations

import json
import re
from datetime import date

from agent import docket
from agent.db import Database
from agent.forms import FORMS, ROLES, oversized
from agent.items import Items, mentions, ref

# What acts on the owner's behalf, per role, and so can be overtaken by their words.
ACTING = {
    "researcher": ("routine", "rule", "wanted"),
    "director": ("aim", "rule"),
    "manager": ("routine", "rule", "experiment"),
}
AIM_KINDS = ("goal", "project")      # an aim that acts: a pursuit or an idea is the owner's own to move
# The parts of the owner's life that bear on each other: words in one weigh on interpretations in the others.
AREA_GROUPS = (("interests", "not_interested", "opportunities"), ("communication", "interrupt", "schedule", "limits", "name"),
               ("school",), ("who", "compensate", "social"), ("other",))
CLOSING = ("experiment", "aim", "routine", "rule", "wanted")   # forms whose ending holds a lesson
AGING_CAP = 4000
REVIEWED = ("assistant", "director", "researcher", "manager")   # the roles whose runs hold the points


def owner_of(item: dict) -> str:
    """The role a point about an item goes to: its owner, or Director for what no role owns."""
    return item["owner"] if item.get("owner") in REVIEWED else "director"


def _group(area: str | None) -> tuple[str, ...]:
    return next((g for g in AREA_GROUPS if (area or "other") in g), ("other",))
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


async def interpretations(db: Database, role: str, areas: set[str]) -> list[dict]:
    """The role's own current conclusions and assumptions about the owner in these areas (not their words)."""
    rows = await Items(db).find("belief", status="current", limit=500,
                                where="json_extract(source, '$.type') != 'owner' AND kind != 'practice'")
    return [b for b in rows if owner_of(b) == role and (b.get("area") or "other") in areas]


async def owner_words_changed(db: Database, item: dict, force: bool = False) -> None:
    """Items watcher: a belief from the owner was made, or changed status (replaced, withdrawn)."""
    if item["form"] != "belief" or (item.get("source") or {}).get("type") != "owner" or item["kind"] == "practice":
        return
    last = await db.fetchone("SELECT status, changes FROM item_log WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item["id"],))
    created = bool(last and last["changes"] and '"created"' in last["changes"])
    if not (force or created or (last and last["status"])):
        return   # an observation added, a field corrected: the words are the same
    for role in REVIEWED:
        open_row = await db.fetchone("SELECT refs FROM docket WHERE role = ? AND key = 'aging' AND status = 'open'", (role,))
        ids = json.loads(open_row["refs"]) if open_row else []
        if ref(item) not in ids:
            ids.append(ref(item))
        beliefs = [b for b in [await Items(db).get(int(i[2:])) for i in ids] if b]
        beliefs.sort(key=lambda b: b["updated_at"], reverse=True)   # the latest words first: they weigh most
        areas = {a for b in beliefs for a in _group(b.get("area"))}
        mine = await work(db, role)
        theirs = [b for b in await interpretations(db, role, areas) if b["id"] not in {x["id"] for x in beliefs}]
        if not mine and not theirs:
            continue
        words = "\n".join(f"- {ref(b)} {b['title'][:160]}" + ("" if b["status"] == "current" else f" ({b['status']})") for b in beliefs)
        parts = [_fit(f"Ägarens ord som ändrats:\n{words}", 1500)]
        if theirs:
            parts.append(_fit("Dina egna tolkningar och antaganden om samma del av ägarens liv (do they still hold beside the owner's "
                              "words? Revise, supersede or retract what they contradict):\n"
                              + "\n".join(f"- {Items.line(b)}" for b in theirs), 1200))
        if mine:
            parts.append(_fit("Ditt arbete som gör något för ägaren (does any of it still search, watch, filter or aim for what they no "
                              "longer want, or miss what they now want?):\n" + "\n".join(f"- {_work_line(w)}" for w in mine), 1700))
        basis = "\n\n".join(parts) + "\nChange what is touched, and answer with what you changed, or 'nothing' if nothing is."
        await docket.raise_point(db, role, "aging", "aging", f"Ägarens ord har ändrats ({len(beliefs)}): berörs ditt arbete eller dina tolkningar?",
                                 basis, ids, weight=2, cap=AGING_CAP + 200)


def _fit(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    cut = text[:cap].rsplit("\n", 1)[0]
    return cut + f"\n…({text.count(chr(10)) - cut.count(chr(10))} rader till: find)"


async def seed_aging(db: Database, days: int = 7) -> int:
    """Once, when the points begin: the owner's words of the last days are set beside the work written before them, as
    the shown-once review of omgång 15 let them slide by unanswered."""
    since = f"-{days} days"   # made, or changed status, then: not merely touched (a migration touched them all)
    rows = await Items(db).find("belief", where="json_extract(source, '$.type') = 'owner' AND kind != 'practice' AND id IN "
                                "(SELECT item_id FROM item_log WHERE status IS NOT NULL AND created_at >= datetime('now', ?))",
                                args=(since,), order="id", limit=500)
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
    role = owner_of(item)
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


# --- stale next steps ------------------------------------------------------------------------

FORWARD = ("next_step", "after", "done_when")   # the fields that say what comes next
STALE_FORMS = ("aim", "step", "experiment", "question", "wanted")
NEW_STALE = 5
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "okt": 10, "oct": 10,
          "nov": 11, "dec": 12}
DAY = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b|\b(\d{1,2})(?:\s?[–-]\s?(\d{1,2}))?\s(jan|feb|mar|apr|maj|may|jun|jul|aug|sep|okt|oct|nov|dec)[a-z]*\b", re.I)


def days_in(text: str, today: date) -> list[date]:
    """The days a text names ('2026-11-01', '1 nov', '17–18 nov': the last of a span), in the year that puts them
    nearest today."""
    out = []
    for m in DAY.finditer(text or ""):
        try:
            if m.group(1):
                out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
                continue
            month, day = MONTHS[m.group(6).lower()[:3]], int(m.group(5) or m.group(4))
            d = date(today.year, month, day)
            if (d - today).days > 183:
                d = d.replace(year=today.year - 1)
            elif (today - d).days > 183:
                d = d.replace(year=today.year + 1)
            out.append(d)
        except ValueError:
            continue
    return out


async def passed_in(db: Database, item: dict, today: date) -> tuple[list[str], list[str]]:
    """What an item's next step names that has passed: (signature, lines in words)."""
    sig, lines = [], []
    for field in FORWARD:
        text = item.get(field)
        if not text:
            continue
        for form, n in sorted(mentions(text)):
            other = await Items(db).get(n)
            if other and other["form"] == form and other["status"] in FORMS[form].closed:
                sig.append(ref(other))
                lines.append(f"{ref(other)} är {other['status']}: {other['title'][:90]}")
        days = sorted(set(days_in(text, today)))
        if days and max(days) < today:   # every day it names has passed (one still ahead: the step still points forward)
            sig.append(max(days).isoformat())
            lines.append(f"{max(days):%-d %b} har passerat, och inget datum framåt")
    return sig, lines


async def sweep_stale(db: Database, role: str, today: date | None = None) -> int:
    """Before a role's run: its open items whose next step names what has closed or passed become points, once per
    such state (answered 'kept', the same state isn't asked again; a new closed thing or day is)."""
    today = today or date.today()
    raised = 0
    for form in STALE_FORMS:
        for item in await Items(db).find(form, open_only=True, limit=500):
            if owner_of(item) != role:
                continue
            key = f"stale:{item['id']}"
            sig, lines = await passed_in(db, item, today)
            if not sig:
                await docket.resolve(db, key, "nästa steg är aktuellt nu", role)
                continue
            sig = sorted(set(sig))
            if await db.fetchone("SELECT 1 FROM docket WHERE key = ? AND status = 'answered' AND refs = ?", (key, json.dumps(sig))):
                continue
            if not await db.fetchone("SELECT 1 FROM docket WHERE key = ? AND status = 'open'", (key,)) and raised >= NEW_STALE:
                continue
            fields = "\n".join(f"{f}: {item[f]}" for f in FORWARD if item.get(f))
            await docket.raise_point(
                db, role, key, "stale", f"Nästa steg i {ref(item)} {item['title'][:70]} hänvisar till det som passerat",
                f"{fields}\n\n" + "\n".join(f"- {x}" for x in lines) + "\nWrite the next step as it stands now (update, with a note; "
                "the old text is kept in the history) and answer 'changed', or answer 'kept' if it holds as written.", sig)
            raised += 1
    return raised


async def resolve_fixed(db: Database, item: dict) -> None:
    """Items watcher: an oversized point clears when its item is within size again, or closed."""
    if oversized(item) and item["status"] not in FORMS[item["form"]].closed:
        return
    await docket.resolve(db, f"oversized:{item['id']}", "inom storleken nu")


WATCHERS = (owner_words_changed, closed, resolve_fixed)
