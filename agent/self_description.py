"""How Audax describes itself (the `how_i_work` tool): read straight from docs/MODEL.md, so it can never drift from
the model. The status line and the build list are for the maintainer, not for the owner, and are left out."""

from __future__ import annotations

import re

from agent.config import ROOT

MODEL = ROOT / "docs" / "MODEL.md"


def describe() -> str:
    text = MODEL.read_text()
    text = re.sub(r"(?m)^Status:.*\n", "", text)
    text = re.split(r"(?m)^## Från i dag till modellen", text)[0]
    return (
        "[internal] Reference, not a script: how Audax works, from its model (docs/MODEL.md). You are the Assistant in it. "
        "Give the essence at the depth the question asks, in your own words and plain text; for a general 'how do you work', "
        "a few lines on who does what and how a finding reaches the owner, then offer more. Most findings are left out, a "
        "few are saved for later, and only what earns it is sent.\n\n" + text.strip()
    )
