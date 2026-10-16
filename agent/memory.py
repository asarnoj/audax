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

    def is_new(self) -> bool:
        """True until the owner has told Audax anything: profile.md is still the template (or missing)."""
        from agent.config import ROOT

        p, template = self.path / "profile.md", ROOT / "templates" / "memory" / "profile.md"
        return not p.exists() or (template.exists() and p.read_text().strip() == template.read_text().strip())

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

    async def commit_if_changed(self, message: str, by: str | None = None) -> list[str]:
        """Saves what changed, tagged with the run that changed it (#R), and records it in the office's log."""
        from agent import trail

        files = await self.changed_files()
        if not files:
            return []
        await self._git("add", "-A")
        code, out = await self._git("commit", "-q", "-m", message + trail.tag())
        if code != 0:
            log.warning("memory commit failed: %s", out)
            return []
        log.info("memory commit (%s): %s", message, ", ".join(files))
        await trail.emit("save", by, files=files[:12])
        return files
