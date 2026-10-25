"""Search (instance/granskning/2026-10-20-omgang-4-rosten.md, "Vad chatten minns"): the conversation, the posts, the
findings and the items in one full-text index, so what was said is found rather than remembered.

Trigrams make it tolerate inflection and parts of words ("track" finds "trackval", "okt" finds "oktober"). Each hit
says who said it and when, which is what "varifrån kom det?" needs: on 19 Oct the old word match didn't find the
owner's own message, and the chat told them it was nothing they had said.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.db import Database

WHO = {"assistant": "assistenten", "director": "Director", "manager": "Manager", "researcher": "Researcher",
       "office": "kontoret", "system": "systemet", "": "kontoret"}   # the owner is named from the settings, never in the code
STOP = {"och", "att", "det", "som", "för", "med", "har", "inte", "den", "jag", "till", "var", "kan", "vad", "the", "and",
        "från", "när", "hur", "eller", "ska", "skulle", "om", "på", "du", "vi", "ni", "är", "fick", "finns", "något", "några"}


@dataclass
class Hit:
    kind: str      # message | post | finding | item
    ref: int
    who: str
    at: str
    text: str      # the passage around the match
    letter: str = ""   # an item's form letter

    def line(self, tz: str = "Europe/Stockholm") -> str:
        when = _local(self.at, tz)
        label = {"message": "", "post": f"post #P{self.ref} ", "finding": f"fynd #F{self.ref} ", "item": f"#{self.letter}{self.ref} "}[self.kind]
        from agent.prompts import owner

        who = owner() if self.who == "owner" else WHO.get(self.who, self.who)
        return f"{when} {label}{who}: {self.text}"


def words_of(query: str) -> list[str]:
    """The words worth searching for: at least three letters (trigrams), not the most common small words."""
    return [w for w in re.findall(r"[\wåäöÅÄÖ:/.\-]+", query.casefold()) if len(w) >= 3 and w not in STOP][:12]


async def search(db: Database, query: str, limit: int = 8, kinds: tuple[str, ...] | None = None,
                 exclude_messages_after: int | None = None) -> list[Hit]:
    """Best matches first. Every word must match; if nothing does, any word may. Ids are 'P12', 'F210' or an item's
    number, as the hit's kind says."""
    words = words_of(query)
    if not words:
        return []
    rows = []
    for joiner in (" AND ", " OR "):
        match = joiner.join('"' + w.replace('"', "") + '"' for w in words)
        sql = "SELECT body, kind, ref, who, at FROM search WHERE search MATCH ?"
        args: list = [match]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        if exclude_messages_after is not None:
            sql += " AND NOT (kind = 'message' AND ref > ?)"
            args.append(exclude_messages_after)
        rows = await db.fetchall(sql + " ORDER BY bm25(search), at DESC LIMIT ?", (*args, limit))
        if rows or len(words) == 1:
            break
    hits = [Hit(r["kind"], int(r["ref"]), r["who"] or "", r["at"] or "", passage(r["body"] or "", words)) for r in rows]
    for h in hits:
        if h.kind == "item" and (row := await db.fetchone("SELECT form FROM items WHERE id = ?", (h.ref,))):
            from agent.forms import FORMS

            h.letter = FORMS[row["form"]].letter
    return hits


def passage(text: str, words: list[str], width: int = 240) -> str:
    """The part of the text around the first matching word, on one line."""
    flat = " ".join(text.split())
    low = flat.casefold()
    at = min((i for i in (low.find(w) for w in words) if i >= 0), default=0)
    start = max(0, at - width // 3)
    end = min(len(flat), start + width)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def _local(utc: str, tz: str) -> str:
    try:
        d = datetime.fromisoformat(utc.replace("Z", "+00:00"))
    except ValueError:
        return utc[:16]
    d = d if d.tzinfo else d.replace(tzinfo=ZoneInfo("UTC"))
    return f"{d.astimezone(ZoneInfo(tz)):%Y-%m-%d %H:%M}"
