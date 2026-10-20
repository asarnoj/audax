"""The last filter before Telegram (instance/granskning/2026-10-20-omgang-4-rosten.md, "Vad som når dig"): the
office's own ids never reach the owner ("Något roligt/genomarbetad idé efter tentan (#B5, bokad)", 19 Oct).

Only what is certainly internal is taken out: ids like #B5, #P12, #S3 and markers like [internal]. English lines
are left alone, since the owner sometimes asks for English text; narration between tool calls never reaches here
in the first place (the chat answers in a reply field, agent/chat.py).
"""

from __future__ import annotations

import logging
import re

from agent.forms import LETTERS

log = logging.getLogger(__name__)

ID = re.compile(r"#[PFBR" + "".join(LETTERS) + r"]\d+\b")
MARKER = re.compile(r"\[(internal|minne|end|internal task[^\]]*)\]", re.I)


def clean(text: str) -> str:
    """The text without the office's ids and markers, and without the brackets and commas they leave behind."""
    out = MARKER.sub("", ID.sub("", text))
    if out == text:
        return text
    out = re.sub(r"[\(\[]\s*[,;/]?\s*", lambda m: m.group(0)[0], out)        # "(, bokad" -> "(bokad"
    out = re.sub(r"\s*[,;/]\s*([\)\]])", r"\1", out)                          # "bokad, )" -> "bokad)"
    out = re.sub(r"\s*(\(\s*\)|\[\s*\])", "", out)                            # empty brackets
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r" +([.,;:!?])", r"\1", out)
    out = "\n".join(line.rstrip() for line in out.splitlines()).strip()
    log.warning("guard: took the office's ids out of a message to the owner")
    return out
