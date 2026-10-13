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
        "[internal] How Audax works, from its model (docs/MODEL.md). Explain it in your own words, at the level of detail "
        "the question calls for; you are the Assistant in it.\n\n" + text.strip()
    )
