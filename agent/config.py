"""Settings and where the instance lives.

The engine (this repo) is generic and public. Everything about one person lives in the instance
directory (default ./instance, gitignored, its own git repo): instance/config/*.yaml. Secrets stay in
.env. Missing instance files are created from config/*.example.yaml on first start.
"""

from __future__ import annotations

import os
from datetime import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
INSTANCE = Path(os.getenv("AUDAX_INSTANCE") or ROOT / "instance")
INSTANCE_FILES = ("settings.yaml", "sources.yaml", "research.yaml", "watchlist.yaml")


def instance_config(name: str) -> Path:
    return INSTANCE / "config" / name


def ensure_instance() -> list[str]:
    """Creates missing instance config from the engine's examples, and gives the instance its own git repo."""
    import shutil
    import subprocess

    created = []
    (INSTANCE / "config").mkdir(parents=True, exist_ok=True)
    for name in INSTANCE_FILES:
        target = instance_config(name)
        if not target.exists():
            shutil.copy(ROOT / "config" / name.replace(".yaml", ".example.yaml"), target)
            created.append(name)
    if not (INSTANCE / ".git").exists():
        git = ["git", "-C", str(INSTANCE), "-c", "user.name=agent", "-c", "user.email=agent@localhost"]
        subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "Init instance from examples"], check=True)
    return created


class QuietHours(BaseModel):
    start: time
    end: time

    def contains(self, t: time) -> bool:
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end  # spans midnight


class Models(BaseModel):
    chat: str = "sonnet"
    triage: str = "haiku"
    dispatcher: str = "haiku"
    research: str = "sonnet"
    manager: str = "sonnet"
    builder: str = "sonnet"
    director: str = "sonnet"
    reflect: str = "opus"


class Paths(BaseModel):
    workspace: Path = Path("workspace")
    database: Path = Path("data/agent.db")


class Site(BaseModel):
    # The office's web page (agent/site.py). Localhost only; Tailscale Serve makes it reachable on the owner's devices.
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8400


class Settings(BaseModel):
    owner: str = ""  # the person the agent is for; fills {owner} in prompts
    timezone: str = "Europe/Stockholm"
    quiet_hours: QuietHours
    models: Models = Models()
    paths: Paths = Paths()
    site: Site = Site()

    telegram_token: str = ""
    telegram_chat_id: int | None = None

    @property
    def workspace(self) -> Path:
        return _resolve(self.paths.workspace)

    @property
    def database(self) -> Path:
        return _resolve(self.paths.database)


def _resolve(p: Path) -> Path:
    return p if p.is_absolute() else ROOT / p


# Env vars that would make Claude Code bill an API account instead of the subscription.
API_BILLING_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def assert_subscription_auth() -> None:
    """The agent must only ever run on the Claude subscription login, never the paid API."""
    found = [v for v in API_BILLING_VARS if os.environ.get(v)]
    if found:
        raise SystemExit(
            f"{', '.join(found)} är satt. Agenten körs bara via Claude-prenumerationen; "
            "ta bort variabeln från miljön/.env och starta igen."
        )


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")
    if path is None:
        path = instance_config("settings.yaml")
        if not path.exists():
            path = ROOT / "config" / "settings.example.yaml"
    data = yaml.safe_load(path.read_text()) or {}

    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    data["telegram_token"] = os.getenv("TELEGRAM_TOKEN", "").strip()
    data["telegram_chat_id"] = int(chat_id) if chat_id else None
    return Settings.model_validate(data)
