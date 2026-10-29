import re
from pathlib import Path

from agent.calls import CALLS

ROOT = Path(__file__).resolve().parent.parent
NOT_CLAUDE = {"insight", "schedule_change", "manager_note", "search_update", "nudge", "clarify", "idea", "preference"}  # outbox candidate and item kinds, not Claude calls


def kinds_in_code() -> set[str]:
    """Every literal in a `kind=` expression across the engine, e.g. kind="research_deep" if deep else "research"."""
    used = set()
    for py in (ROOT / "agent").rglob("*.py"):
        for expr in re.findall(r"\bkind=(f?\"[^,\n)]+)", py.read_text()):  # literal kinds only, not kind=row["kind"]
            if expr.startswith('f"director_{mode}"'):
                used |= {"director_understand", "director_reflect"}
            used |= set(re.findall(r'"([a-z_]+)"', expr))
    return used - NOT_CLAUDE


def test_every_claude_call_in_the_code_is_on_the_map():
    missing = kinds_in_code() - set(CALLS)
    assert not missing, f"Claude calls missing from agent/calls.py: {missing}"
    assert {"chat", "triage", "dispatcher", "research", "research_deep", "manager", "builder"} <= kinds_in_code()
