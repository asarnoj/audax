"""Prompts are read on every use, so edits take effect without a restart.

The owner's name is never written into the engine: prompts use {owner} and {owner_s} (genitive),
filled in from `owner` in instance/config/settings.yaml.
"""

from pathlib import Path

DIR = Path(__file__).parent
_owner = "användaren"


def set_owner(name: str) -> None:
    global _owner
    _owner = (name or "").strip() or "användaren"


def owner() -> str:
    return _owner


def personalize(text: str) -> str:
    genitive = _owner if _owner[-1:].lower() in ("s", "x", "z") else _owner + "s"
    return text.replace("{owner_s}", genitive).replace("{owner}", _owner)


def prompt(name: str) -> str:
    return personalize((DIR / f"{name}.md").read_text())
