"""What is old must not steer as if it were new (instance/granskning/2026-11-09-omgang-13-levande-kunskap.md, D).

Before a role's run its texts and working memory are read for references to things that have since closed, been
replaced or passed (Manager's working memory said "#E201 pågår till 9 nov" two nights after #E201 ended), and for
lines in the texts that steer what Audax does without resting on anything (a rule in the policy with no belief
behind it). Both are shown in the role's review; nothing is changed for it.

A line that cites something must say what it says (omgång 16, C): the policy said "lectures no longer stop
anything (#U168)" while #U168, the owner's own words, said the opposite. Whether a line agrees with what it cites is
judgement, so the code doesn't decide it: it sets each line that cites a belief, or anything closed, beside the
words of what it cites, as a point for the text's owner (agent/docket.py). Once answered, that line and citation
aren't asked about again; a line that changes, or a citation that closes, is.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from agent.db import Database
from agent.forms import FORMS, LETTERS
from agent.items import Items
from agent.texts import owned_by

REF = re.compile(r"#([A-Z])(\d+)\b")
STEERS = ("policy.md", "direction/now.md")   # texts whose lines steer what the office does
SHOWN = 12


def _texts(memory: Path, role: str) -> list[tuple[str, str]]:
    out = []
    for t in owned_by(role):
        if t.owner == "all":
            continue
        for path in sorted(memory.glob(t.pattern)):
            text = re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.DOTALL)
            text = text.split("## Datum (skrivs av koden)")[0]   # the code's own dates are current by construction
            out.append((str(path.relative_to(memory)), text))
    return out


async def stale(db: Database, memory: Path, role: str) -> list[str]:
    """References in the role's texts to items that are closed, replaced or past their date."""
    seen, out = set(), []
    for name, text in _texts(memory, role):
        for letter, num in REF.findall(text):
            form = LETTERS.get(letter)
            if form is None or (letter, num) in seen:
                continue
            seen.add((letter, num))
            row = await db.fetchone("SELECT id, form, title, status, updated_at, valid_until FROM items WHERE id = ? AND form = ?",
                                    (int(num), form))
            if row is None:
                continue
            closed = row["status"] in FORMS[row["form"]].closed
            if closed:
                out.append(f"#{letter}{num} i {name} är {row['status']} sedan {row['updated_at'][:10]}: {row['title'][:80]}")
    return out[:SHOWN]


def unanchored(memory: Path, role: str) -> list[str]:
    """Lines in the texts that steer (the policy, now.md) and refer to nothing: a rule with no belief behind it."""
    out = []
    for name, text in _texts(memory, role):
        if name not in STEERS:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith(("- ", "* ")) and not REF.search(line):
                out.append(f"{name}: {line[2:][:140]}")
    return out[:SHOWN]


CITE_LINES = 10   # lines per point; the rest come in the next run


async def cite_points(db: Database, memory: Path, role: str) -> int:
    """Each of the role's texts with lines citing a belief or something closed, not yet checked: one point per text."""
    from agent import docket
    from agent.beliefs import weight

    raised = 0
    for name, text in _texts(memory, role):
        key = f"cite:{name}"
        checked = set()
        for r in await db.fetchall("SELECT refs FROM docket WHERE key = ? AND role = ? AND status = 'answered'", (key, role)):
            checked |= set(json.loads(r["refs"] or "[]"))
        shown, pairs = [], []
        for line in text.splitlines():
            line = line.strip()
            cited = []
            for letter, num in dict.fromkeys(REF.findall(line)):
                form = LETTERS.get(letter)
                row = await db.fetchone("SELECT * FROM items WHERE id = ? AND form = ?", (int(num), form)) if form else None
                if row is None:
                    continue
                closed = row["status"] in FORMS[form].closed
                if form != "belief" and not closed:
                    continue
                pair = f"{hashlib.sha1(line.encode()).hexdigest()[:10]}:#{letter}{num}"
                if pair in checked:
                    continue
                said = weight(await Items(db).get(row["id"])) if form == "belief" else ""
                cited.append((pair, f"#{letter}{num} ({row['status']}{', ' + said if said else ''}): {row['title'][:160]}"))
            if cited:
                shown.append(f"[{len(shown) + 1}] {line[:240]}\n" + "\n".join(f"  → {c}" for _, c in cited))
                pairs += [p for p, _ in cited]
            if len(shown) >= CITE_LINES:
                break
        if not shown:
            await docket.resolve(db, key, "alla hänvisningar avstämda", role)
            continue
        await docket.raise_point(
            db, role, key, "cite", f"Säger raderna i {name} det de hänvisar till?",
            "Each line, and beside it the words and state of what it cites. Read each line against its own sources: does "
            "it say what they say?\n" + "\n".join(shown)
            + f"\nWhere a line says something other than what it cites, or leans on what has closed or been replaced, change "
            f"the line (text: {name}) or the item and answer 'changed' with its number in `off`; 'kept' with off [] if "
            "every line holds.", pairs, cap=3500)
        raised += 1
    return raised


async def for_review(db: Database, memory: Path | None, role: str) -> str:
    if memory is None:
        return ""
    parts = []   # what cites the closed or a belief is a point now (cite_points)
    if loose := unanchored(memory, role):
        parts.append("Rader som styr utan att vila på något (each rule or filter rests on a belief, #U: about the owner, or a "
                     "practice of the office, with where it came from and its observations. Make the belief and refer to it, "
                     "or drop the line if nothing supports it):\n" + "\n".join(f"- {x}" for x in loose))
    return "\n\n".join(parts)
