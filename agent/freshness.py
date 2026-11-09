"""What is old must not steer as if it were new (instance/granskning/2026-11-09-omgang-13-levande-kunskap.md, D).

Before a role's run its texts and working memory are read for references to things that have since closed, been
replaced or passed (Manager's working memory said "#E201 pågår till 9 nov" two nights after #E201 ended), and for
lines in the texts that steer what Audax does without resting on anything (a rule in the policy with no belief
behind it). Both are shown in the role's review; nothing is changed for it.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent.db import Database
from agent.forms import FORMS, LETTERS
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


async def for_review(db: Database, memory: Path | None, role: str) -> str:
    if memory is None:
        return ""
    parts = []
    if old := await stale(db, memory, role):
        parts.append("Inaktuellt i dina texter (they refer to something that has closed: is what you wrote still true?):\n"
                     + "\n".join(f"- {x}" for x in old))
    if loose := unanchored(memory, role):
        parts.append("Rader som styr utan att vila på något (each rule or filter rests on a belief, #U: about the owner, or a "
                     "practice of the office, with where it came from and its observations. Make the belief and refer to it, "
                     "or drop the line if nothing supports it):\n" + "\n".join(f"- {x}" for x in loose))
    return "\n\n".join(parts)
