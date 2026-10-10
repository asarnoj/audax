"""Build mode: the owner asks for a change in chat; Claude Code makes it on a git branch in a separate
worktree; tests run; the owner approves with a button before anything goes live.

Safety rails (verified with a live probe):
- Claude works in a throwaway worktree under /var/tmp, outside the home directory. File tools are
  allowed there and denied for all of ~ (where .env, ~/.ssh, Claude credentials, data and memory live).
- Bash is limited to read-only git. Claude never runs code: the system runs the tests and, if they
  fail, hands the output back to Claude for another attempt.
- The system, not Claude, commits and merges.
- Nothing is merged without the owner pressing "Driftsätt".
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import ROOT, Settings
from agent.db import Database
from agent.prompts import prompt as prompt_template

log = logging.getLogger(__name__)

NOTICE_KEY = "builder.deploy_notice"
TOOLS = ["Read(./**)", "Edit(./**)", "Write(./**)", "Glob", "Grep", "Bash(git status)", "Bash(git diff *)", "Bash(git log *)"]
DENY = ["Read(~/**)", "Edit(~/**)", "Write(~/**)"]
FIX_ROUNDS = 2
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "needs_followup": {"type": ["string", "null"]},
    },
    "required": ["summary", "risk"],
}
# Changes under these paths are picked up live; anything else needs a restart.
LIVE_RELOAD = ("config/", "agent/prompts/", "docs/", "tests/", "README.md")

OnReady = Callable[[int], Awaitable[None]]
Notify = Callable[[str], Awaitable[object]]


async def sh(*args: str, cwd: Path, timeout: float = 900) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}  # let uv use the worktree's own venv
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(cwd), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"timeout after {timeout:.0f}s"
    return proc.returncode, out.decode(errors="replace")


async def git(*args: str, cwd: Path) -> tuple[int, str]:
    return await sh("git", "-c", "user.name=agent-builder", "-c", "user.email=agent@localhost", *args, cwd=cwd, timeout=120)


class Builder:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        gate: ClaudeGate,
        on_ready: OnReady,
        notify: Notify,
        repo: Path = ROOT,
        verify: Callable[[Path], Awaitable[tuple[bool, str]]] | None = None,
    ):
        self.settings = settings
        self.db = db
        self.gate = gate  # its own gate: a build doesn't block the chat
        self.repo = repo
        self.builds = Path("/var/tmp") / f"{repo.name.replace(' ', '-')}-builds"  # outside ~, see DENY
        self._on_ready = on_ready
        self._verify = verify or self.run_tests
        self._notify = notify
        self._background: set[asyncio.Task] = set()

    def _worktree(self, change_id: int) -> Path:
        return self.builds / f"change-{change_id}"

    @staticmethod
    def _branch(change_id: int) -> str:
        return f"change/{change_id}"

    # --- request ----------------------------------------------------------

    async def request(self, title: str, description: str) -> int:
        busy = await self.db.fetchone("SELECT id FROM changes WHERE status IN ('queued', 'building')")
        if busy:
            raise RuntimeError(f"ändring {busy['id']} byggs redan")
        change_id = await self.db.execute("INSERT INTO changes (title, request) VALUES (?, ?)", (title, description))
        task = asyncio.create_task(self._build(change_id))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return change_id

    async def _set(self, change_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self.db.execute(f"UPDATE changes SET {cols} WHERE id = ?", (*fields.values(), change_id))

    async def _build(self, change_id: int) -> None:
        row = await self.db.fetchone("SELECT * FROM changes WHERE id = ?", (change_id,))
        wt, branch = self._worktree(change_id), self._branch(change_id)
        await self._set(change_id, status="building")
        try:
            self.builds.mkdir(exist_ok=True)
            code, out = await git("worktree", "add", "-b", branch, str(wt), "HEAD", cwd=self.repo)
            if code != 0:
                raise RuntimeError(f"git worktree: {out.strip()}")

            result = await self._claude(prompt_template("builder").format(title=row["title"], request=row["request"]), wt)
            out_ = result.structured

            await git("add", "-A", cwd=wt)
            code, _ = await git("diff", "--cached", "--quiet", cwd=wt)
            if code == 0:  # nothing changed
                await self._set(change_id, status="failed", summary=out_["summary"], risk=out_["risk"])
                await self._cleanup(change_id)
                await self._notify(f"Ändring {change_id} ({row['title']}): jag gjorde ingen ändring.\n\n{out_['summary']}")
                return
            tests_ok, test_out = await self._verify(wt)
            for _ in range(FIX_ROUNDS):
                if tests_ok:
                    break
                tail = "\n".join(test_out.strip().splitlines()[-60:])
                fix = await self._claude(
                    f"Testerna gick inte igenom efter din ändring. Rätta felet (ändra inte testerna bara för att få dem gröna, "
                    f"om inte testet självt är fel). Utdata:\n\n{tail}",
                    wt,
                    resume=result.session_id,
                )
                out_ = fix.structured or out_
                tests_ok, test_out = await self._verify(wt)

            await git("add", "-A", cwd=wt)
            await git("commit", "-q", "-m", f"{row['title']}\n\n{row['request']}\n\nBuilt by agent build mode (change {change_id}).", cwd=wt)
            _, diffstat = await git("diff", "--stat", "HEAD~1", "HEAD", cwd=wt)
            summary = out_["summary"] + (f"\n\nAtt göra för dig: {out_['needs_followup']}" if out_.get("needs_followup") else "")
            await self._set(
                change_id,
                status="ready" if tests_ok else "failed",
                summary=summary,
                risk=out_["risk"],
                diffstat=diffstat.strip(),
                test_output=test_out[-3000:],
            )
        except Exception as e:
            log.exception("build %s failed", change_id)
            await self._set(change_id, status="failed", summary=f"Bygget misslyckades: {e}")
            await self._cleanup(change_id)
        await self._on_ready(change_id)

    async def _claude(self, prompt: str, wt: Path, resume: str | None = None):
        try:
            result = await self.gate.run(
                ClaudeTask(
                    kind="builder",
                    prompt=prompt,
                    priority=Priority.MANAGER,
                    model=self.settings.models.builder,
                    allowed_tools=TOOLS,
                    disallowed_tools=DENY,
                    output_schema=SCHEMA,
                    max_turns=80,
                    cwd=wt,
                    resume=resume,
                )
            )
        except QuotaPaused:
            raise RuntimeError("Claude-kvoten är slut just nu, försök igen senare")
        if result.is_error or not isinstance(result.structured, dict):
            raise RuntimeError(result.error or "inget strukturerat svar")
        return result

    async def run_tests(self, wt: Path) -> tuple[bool, str]:
        """The system runs the tests itself; Claude saying they pass is not enough."""
        code, out = await sh("uv", "run", "--frozen", "pytest", "-q", cwd=wt)
        if code != 0:
            return False, out
        code2, out2 = await sh("uv", "run", "--frozen", "python", "-c", "import agent.main", cwd=wt, timeout=120)
        return code2 == 0, out + ("" if code2 == 0 else "\nimport agent.main misslyckades:\n" + out2)

    async def diff(self, change_id: int) -> str:
        code, out = await git("diff", "HEAD~1", "HEAD", cwd=self._worktree(change_id))
        return out if code == 0 else ""

    # --- decide -----------------------------------------------------------

    async def deploy(self, change_id: int) -> str:
        row = await self.db.fetchone("SELECT * FROM changes WHERE id = ?", (change_id,))
        if row is None or row["status"] != "ready":
            return "Den ändringen går inte att driftsätta (redan avgjord eller inte klar)."
        code, out = await git("status", "--porcelain", "--untracked-files=no", cwd=self.repo)
        if out.strip():
            return "Det finns ocommittade ändringar i huvudrepot, så jag rör inget. Kolla på Pi:n."
        code, out = await git("merge", "--no-edit", self._branch(change_id), cwd=self.repo)
        if code != 0:
            await git("merge", "--abort", cwd=self.repo)
            return f"Kunde inte slå ihop ändringen (konflikt). Inget är ändrat.\n{out[-500:]}"

        _, changed = await git("diff", "--name-only", "HEAD~1", "HEAD", cwd=self.repo)
        files = [f for f in changed.splitlines() if f]
        await self._set(change_id, status="deployed")
        await self.db.execute("UPDATE changes SET decided_at = datetime('now') WHERE id = ?", (change_id,))
        await self._cleanup(change_id, delete_branch=True)

        if any(f in ("pyproject.toml", "uv.lock") for f in files):
            await sh("uv", "sync", "--no-dev", "-q", cwd=self.repo)
        if all(f.startswith(LIVE_RELOAD) for f in files):
            return "Driftsatt. Det gäller direkt, ingen omstart behövdes."

        await self.db.kv_set(NOTICE_KEY, row["title"])
        if os.environ.get("INVOCATION_ID"):  # running under systemd
            await sh("sudo", "-n", "systemctl", "restart", "--no-block", "agent", cwd=self.repo, timeout=30)
            return "Driftsatt. Jag startar om nu och hör av mig när jag är tillbaka."
        return "Driftsatt i koden. Starta om agenten för att den ska gälla."

    async def discard(self, change_id: int) -> str:
        row = await self.db.fetchone("SELECT status FROM changes WHERE id = ?", (change_id,))
        if row is None or row["status"] in ("deployed", "discarded"):
            return "Den ändringen är redan avgjord."
        await self._set(change_id, status="discarded")
        await self.db.execute("UPDATE changes SET decided_at = datetime('now') WHERE id = ?", (change_id,))
        await self._cleanup(change_id, delete_branch=True)
        return "Slängd. Inget ändrades."

    async def _cleanup(self, change_id: int, delete_branch: bool = False) -> None:
        wt = self._worktree(change_id)
        if wt.exists():
            await git("worktree", "remove", "--force", str(wt), cwd=self.repo)
            shutil.rmtree(wt, ignore_errors=True)
        await git("worktree", "prune", cwd=self.repo)
        if delete_branch:
            await git("branch", "-D", self._branch(change_id), cwd=self.repo)

    # --- startup ------------------------------------------------------------

    async def on_startup(self) -> None:
        """Announces a deploy that required a restart, and fails builds interrupted by one."""
        if title := await self.db.kv_get(NOTICE_KEY):
            await self.db.kv_set(NOTICE_KEY, None)
            await self._notify(f"Tillbaka. Ändringen \"{title}\" är live.")
        for r in await self.db.fetchall("SELECT id FROM changes WHERE status IN ('queued', 'building')"):
            await self._set(r["id"], status="failed", summary="Avbröts av en omstart.")
            await self._cleanup(r["id"], delete_branch=True)
