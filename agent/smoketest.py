"""Checks that the machine is ready: `uv run python -m agent.smoketest [--skip-claude]`."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from telegram import Bot

from agent.claude_gate import ClaudeGate, ClaudeTask, Priority
from agent.config import API_BILLING_VARS, ensure_instance, load_settings
from agent.db import Database

OK, FAIL, WARN = "✅", "❌", "⚠️ "


async def main(skip_claude: bool) -> int:
    failures = 0
    ensure_instance()
    settings = load_settings()

    claude = shutil.which("claude") or next(
        (str(p) for p in [Path.home() / ".local/bin/claude"] if p.exists()), None
    )
    print(f"{OK if claude else WARN} claude CLI: {claude or 'saknas i PATH (SDK:ns medföljande CLI används om den finns)'}")

    # Billing: must be the subscription login, never an API key.
    api_vars = [v for v in API_BILLING_VARS if os.environ.get(v)]
    if api_vars:
        failures += 1
        print(f"{FAIL} {', '.join(api_vars)} är satt: agenten skulle debitera API:et")
    if claude:
        try:
            auth = json.loads(subprocess.run([claude, "auth", "status"], capture_output=True, text=True, timeout=30).stdout)
            sub_ok = auth.get("authMethod") == "claude.ai"
            failures += 0 if sub_ok else 1
            print(f"{OK if sub_ok else FAIL} Claude-inloggning: {auth.get('authMethod')} ({auth.get('subscriptionType')})")
        except Exception as e:
            print(f"{WARN} kunde inte läsa `claude auth status`: {e}")

    if settings.telegram_token:
        try:
            me = await Bot(settings.telegram_token).get_me()
            print(f"{OK} Telegram-bot: @{me.username}")
        except Exception as e:
            failures += 1
            print(f"{FAIL} Telegram: {e}")
    else:
        failures += 1
        print(f"{FAIL} TELEGRAM_TOKEN saknas i .env")
    print(f"{OK if settings.telegram_chat_id else WARN} TELEGRAM_CHAT_ID: {settings.telegram_chat_id or 'inte satt än (skriv /start till boten)'}")

    # Calendars: fetch the coming week from each configured source.
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from agent.sources.calendar import build_sources, load_calendar_configs

    configs, _ = load_calendar_configs()
    for c in configs:
        if not c.configured:
            print(f"{WARN} kalender {c.id}: inte konfigurerad i .env")
    now = datetime.now(ZoneInfo(settings.timezone))
    for src in build_sources(configs, settings.timezone):
        try:
            evs = await src.fetch(now, now + timedelta(days=7))
            print(f"{OK} kalender {src.config.id}: {len(evs)} händelser kommande vecka, {len({e.series_key for e in evs})} sorters pass")
        except Exception as e:
            failures += 1
            print(f"{FAIL} kalender {src.config.id}: {type(e).__name__}: {e}")

    if not skip_claude:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "smoke.db")
            await db.open()
            gate = ClaudeGate(db, settings.workspace)
            r = await gate.run(
                ClaudeTask(kind="smoketest", prompt="Svara exakt: hej", priority=Priority.CHAT, model=settings.models.triage, max_turns=1)
            )
            await db.close()
        if r.is_error:
            failures += 1
            print(f"{FAIL} Claude: {r.error}")
        else:
            print(f"{OK} Claude svarade {r.text.strip()!r} på {r.duration_ms / 1000:.1f}s")
            if gate.last_rate_limit:
                rl = gate.last_rate_limit
                print(f"   kvot: {rl.status} ({rl.rate_limit_type}, {rl.utilization})")

    return failures


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main("--skip-claude" in sys.argv)) else 0)
