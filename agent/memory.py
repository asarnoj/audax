"""workspace/memory: markdown files in their own local git repo, so every change can be reviewed and undone."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


class MemoryRepo:
    def __init__(self, path: Path):
        self.path = path

    def read(self, *names: str) -> str:
        """Concatenates memory files for injection into prompts, without template comments."""
        parts = []
        for name in names:
            p = self.path / name
            if p.exists():
                text = re.sub(r"<!--.*?-->", "", p.read_text(), flags=re.DOTALL).strip()
                parts.append(f"--- {name} ---\n{text}")
        return "\n\n".join(parts)

    async def _git(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(self.path), "-c", "user.name=agent", "-c", "user.email=agent@localhost", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode, out.decode().rstrip("\n")

    async def changed_files(self) -> list[str]:
        code, out = await self._git("status", "--porcelain")
        if code != 0:
            log.warning("memory git status failed: %s", out)
            return []
        return [line[3:] for line in out.splitlines() if line]

    async def commit_if_changed(self, message: str) -> list[str]:
        files = await self.changed_files()
        if not files:
            return []
        await self._git("add", "-A")
        code, out = await self._git("commit", "-q", "-m", message)
        if code != 0:
            log.warning("memory commit failed: %s", out)
            return []
        log.info("memory commit (%s): %s", message, ", ".join(files))
        return files
