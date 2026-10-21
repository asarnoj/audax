"""Layer 3, the loose text (instance/granskning/2026-10-20-omgang-5-texten-och-flytten.md, "Vad som blir kvar som text"):
interpretations, reasoning, tone and lessons. Three principles, not locks:

1. The text refers, it doesn't copy: facts that live in layer 2 (a deadline, a decision, something the owner said)
   are there with an id and not repeated here.
2. The text is changed in parts, not as a whole file: a role changes one section at a time (`edit`), so nothing is
   lost by a rewrite (the Google section vanished from now.md on 19 Oct).
3. Every file has an owner, a purpose and a rough size; one that has grown past it shows up in its owner's review.

The profile is not here: it is a view the code writes from the beliefs (agent/beliefs.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

GENERATED = "(skrivs av koden)"   # a heading with this is written by the code, never by a role


@dataclass(frozen=True)
class Text:
    pattern: str   # path under memory/, may hold one *
    owner: str     # a role, or "all": every role adds its own lines
    purpose: str
    size: int      # characters it should stay under, roughly


TEXTS = (
    Text("direction/person.md", "director",
         "The story of the owner: what drives them, patterns, how the picture has changed. Refers to beliefs (#U) "
         "rather than quoting them.", 5000),
    Text("direction/now.md", "director",
         "The situation in Director's own words: what is going on and what chafes. The dates are added by the code.", 3000),
    Text("policy.md", "manager",
         "The reasoning about the owner's attention and the tone, referring to the experiments (#E). The owner's own "
         "wishes about style stay verbatim under their own heading.", 3500),
    Text("arbetsminne/director.md", "director", "Working memory: focus, open hypotheses, waiting for, tried and learned. Ids only.", 2000),
    Text("arbetsminne/manager.md", "manager", "Working memory: focus, open hypotheses, waiting for, tried and learned. Ids only.", 2000),
    Text("arbetsminne/researcher.md", "researcher", "Working memory: focus, open hypotheses, waiting for, tried and learned. Ids only.", 2000),
    Text("arbetsminne/assistant.md", "assistant", "Working memory: ongoing threads, what the owner cares about now, how they want it. Ids only.", 2000),
    Text("research/maps/*.md", "researcher", "How to search one space: segments, where they publish, methods, gaps.", 4000),
    Text("lardomar.md", "all", "The office's lessons about how to work that can't run as a rule in the code.", 4000),
)


class TextError(ValueError):
    pass


def text_of(name: str) -> Text | None:
    return next((t for t in TEXTS if fnmatch(name, t.pattern)), None)


def owned_by(role: str) -> list[Text]:
    return [t for t in TEXTS if t.owner in (role, "all")]


def describe(role: str) -> str:
    return "\n".join(f"- {t.pattern} (~{t.size} chars): {t.purpose}" for t in owned_by(role))


def _sections(text: str) -> list[tuple[str, int, int]]:
    """(heading, start, end) for each level-2 section, end exclusive, by line."""
    lines = text.splitlines()
    heads = [i for i, line in enumerate(lines) if re.match(r"##\s", line)]
    return [(lines[i][2:].strip(), i, heads[n + 1] if n + 1 < len(heads) else len(lines)) for n, i in enumerate(heads)]


def edit(memory: Path, role: str, name: str, section: str, text: str | None) -> str:
    """Replaces one `## section` of a text the role owns (adds it at the end if it isn't there); text None or empty
    removes it. Returns what was done, for the role."""
    name = name.strip().lstrip("/").removeprefix("memory/")
    t = text_of(name)
    if t is None:
        raise TextError(f"{name} is not one of the texts: " + ", ".join(x.pattern for x in TEXTS))
    if t.owner == "all":
        raise TextError(f"{name} only grows: add to it with `lessons` in your answer")
    if t.owner != role:
        raise TextError(f"{name} is {t.owner}'s; send them a post instead")
    section = section.strip().lstrip("#").strip()
    if not section:
        raise TextError("give the section's heading")
    if GENERATED in section:
        raise TextError("that section is written by the code")
    path = memory / name
    if ".." in Path(name).parts:
        raise TextError("not a path under memory/")
    old = path.read_text() if path.exists() else f"# {Path(name).stem.capitalize()}\n"
    lines = old.splitlines()
    match = next(((h, a, b) for h, a, b in _sections(old) if h.casefold() == section.casefold()), None)
    body = [f"## {section}", *((text or "").strip().splitlines()), ""] if (text or "").strip() else []
    if match:
        _, a, b = match
        new = lines[:a] + body + lines[b:]
        done = "replaced" if body else "removed"
    elif body:
        gen = next((a for h, a, _ in _sections(old) if GENERATED in h), None)   # own sections go before the code's
        at = gen if gen is not None else len(lines)
        new = lines[:at] + ([""] if at and lines[at - 1].strip() else []) + body + lines[at:]
        done = "added"
    else:
        return f"{name}: no section '{section}', nothing removed"
    out = "\n".join(new).rstrip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(re.sub(r"\n{3,}", "\n\n", out))
    size = len(path.read_text())
    return f"{name}: section '{section}' {done}" + (f" (the file is {size} chars, over its ~{t.size}: tighten it)" if size > t.size else "")


def oversized(memory: Path, role: str) -> list[str]:
    """The role's texts that have grown past their size: for its cleaning pass."""
    out = []
    for t in owned_by(role):
        if t.owner == "all":
            continue
        for path in sorted(memory.glob(t.pattern)):
            n = len(path.read_text())
            if n > t.size:
                out.append(f"{path.relative_to(memory)}: {n} chars, ~{t.size} is its size")
    return out


# The working memory is part of every role's answer, a section at a time, so a run has to consider it (a tool alone
# was skipped: the Manager probe of 21 Oct left "Tis 20 okt" in place).
WORKING_MEMORY = {
    "type": "array",
    "description": "Swedish. The sections of your working memory that changed, each with its whole new content; [] if "
                   "nothing changed. Short, ids rather than copied facts.",
    "items": {"type": "object", "properties": {"section": {"type": "string"}, "text": {"type": "string"}},
              "required": ["section", "text"]},
}


def working_memory(memory: Path, role: str, sections: list[dict] | None) -> list[str]:
    done = []
    for s in sections or []:
        try:
            done.append(edit(memory, role, f"arbetsminne/{role}.md", s.get("section", ""), s.get("text")))
        except TextError as e:
            done.append(f"arbetsminnet: {e}")
    return done
