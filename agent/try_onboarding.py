"""Try the first conversation in the terminal, in a throwaway home: the real chat and prompts, nothing kept.

    ./deploy/audax onboarding            as the owner of their own Audax
    ./deploy/audax onboarding <host>     as a friend on <host>'s machine (the interview says where things are stored)

Write "sluta" (or an empty line) to end. Everything is deleted afterwards: the home, its database and memory, and
Claude Code's session files for it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path


async def main(host: str) -> None:
    name = input("Vilket namn ska ägaren ha i testet? ").strip() or "Testperson"
    home = Path(tempfile.mkdtemp(prefix="audax-try-"))
    os.environ["AUDAX_HOME"] = str(home)   # before the engine is imported: it reads where it lives at import
    os.environ.pop("AUDAX_INSTANCE", None)
    from agent.chat import ChatSession, Incoming
    from agent.claude_gate import ClaudeGate
    from agent.config import ensure_instance, ensure_workspace, load_settings
    from agent.db import Database
    from agent.items import Items
    from agent.memory import MemoryRepo
    from agent.prompts import prompt, set_owner

    db = None
    try:
        ensure_instance()
        settings = load_settings()
        settings.owner, settings.host = name, host
        set_owner(name)
        ensure_workspace(settings.workspace)
        (settings.workspace / "CLAUDE.md").write_text(prompt("claude_md"))
        db = Database(settings.database)
        await db.open()

        async def send(text, **kw):
            print(f"\n\033[1mAudax:\033[0m {text}\n", flush=True)
            return [await db.insert("messages", telegram_message_id=0, direction="out", text=text, origin=kw.get("origin") or "chat",
                                    in_session=int(bool(kw.get("in_session"))))]

        async def typing():
            return None

        chat = ChatSession(settings, db, ClaudeGate(db, settings.workspace), MemoryRepo(settings.workspace / "memory"), send, typing)

        async def idle():
            while chat._worker and not chat._worker.done():
                await asyncio.sleep(0.3)

        print("(skriver …)", flush=True)
        await chat.start_interview()
        await idle()
        while True:
            line = (await asyncio.to_thread(input, "du> ")).strip()
            if line.lower() in ("", "sluta", "exit"):
                break
            row = await db.insert("messages", telegram_message_id=0, direction="in", text=line)
            print("(skriver …)", flush=True)
            chat.submit(Incoming(text=line, message_row_id=row))
            await idle()
        kept = await Items(db).find("belief", status="current", order="id")
        if kept:
            print("\nDet här sparade den om dig (raderas nu):")
            for b in kept:
                print(f"- [{b.get('area') or '?'}] {b['title']}")
    finally:
        if db is not None:
            await db.close()
        shutil.rmtree(home, ignore_errors=True)
        sessions = Path.home() / ".claude" / "projects"
        for d in sessions.glob(f"*{home.name}*"):   # Claude Code's own record of the test conversation
            shutil.rmtree(d, ignore_errors=True)
        print("\nAllt från testet är raderat.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ""))
