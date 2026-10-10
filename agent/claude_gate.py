"""ClaudeGate: the only way the agent talks to Claude.

- One Claude session at a time (Pi 400 has 4 GB RAM; Pro quota is shared with the user).
- Waiting runs are served by priority: chat first, background last.
- Rate-limit signals from Claude Code pause lower-priority work until the window resets.
- Every run is logged to claude_runs.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    TextBlock,
    query,
)

from agent.db import Database

log = logging.getLogger(__name__)


class Priority(IntEnum):
    CHAT = 0
    URGENT = 1
    RESEARCH = 2
    MANAGER = 3
    BACKGROUND = 4


@dataclass
class ClaudeTask:
    kind: str  # chat|triage|dispatcher|research|manager|...
    prompt: str
    priority: Priority
    model: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    resume: str | None = None
    system_prompt_append: str | None = None
    output_schema: dict[str, Any] | None = None
    max_turns: int | None = None
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    cwd: Path | None = None  # defaults to the gate's workspace
    disallowed_tools: list[str] = field(default_factory=list)  # deny rules; win over allowed_tools
    effort: str | None = None  # low|medium|high|xhigh|max


@dataclass
class RunResult:
    text: str
    session_id: str | None = None
    structured: Any = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    is_error: bool = False
    error: str | None = None


class QuotaPaused(Exception):
    def __init__(self, resets_at: float):
        super().__init__(f"Claude quota paused until {time.ctime(resets_at)}")
        self.resets_at = resets_at


OnRateLimit = Callable[[RateLimitInfo], None]
Runner = Callable[[ClaudeTask, Path, OnRateLimit], Awaitable[RunResult]]


async def sdk_runner(task: ClaudeTask, workspace: Path, on_rate_limit: OnRateLimit) -> RunResult:
    """Runs a task through Claude Code via claude-agent-sdk."""
    options = ClaudeAgentOptions(
        cwd=str(task.cwd or workspace),
        model=task.model,
        resume=task.resume,
        allowed_tools=task.allowed_tools,
        disallowed_tools=task.disallowed_tools,
        # Non-interactive: anything not explicitly allowed is denied, never prompted.
        permission_mode="dontAsk",
        # Load workspace/CLAUDE.md and workspace/.claude/settings.json only.
        setting_sources=["project"],
        max_turns=task.max_turns,
        effort=task.effort,
        mcp_servers=task.mcp_servers,
        system_prompt=(
            {"type": "preset", "preset": "claude_code", "append": task.system_prompt_append}
            if task.system_prompt_append
            else None
        ),
        output_format=(
            {"type": "json_schema", "schema": task.output_schema} if task.output_schema else None
        ),
    )

    texts: list[str] = []
    result: ResultMessage | None = None
    async for msg in query(prompt=task.prompt, options=options):
        if isinstance(msg, AssistantMessage):
            texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
        elif isinstance(msg, RateLimitEvent):
            on_rate_limit(msg.rate_limit_info)
        elif isinstance(msg, ResultMessage):
            result = msg

    if result is None:
        return RunResult(text="", is_error=True, error="no result message")
    return RunResult(
        text=result.result or "\n".join(texts),
        session_id=result.session_id,
        structured=result.structured_output,
        cost_usd=result.total_cost_usd,
        duration_ms=result.duration_ms,
        is_error=result.is_error,
        error=("; ".join(map(str, result.errors or [])) or result.subtype) if result.is_error else None,
    )


@dataclass
class QuotaState:
    """Shared between gates, so a rate-limit signal seen by one pauses work in all."""

    # Runs at paused_from or lower priority are refused until paused_until.
    paused_from: Priority | None = None
    paused_until: float = 0.0
    last_rate_limit: RateLimitInfo | None = None


class ClaudeGate:
    """One Claude run at a time through this gate. A second gate (e.g. the builder) gets its own
    slot but shares quota state via `quota`."""

    def __init__(self, db: Database, workspace: Path, runner: Runner = sdk_runner, quota: QuotaState | None = None):
        self.db = db
        self.workspace = workspace
        self._runner = runner
        self._busy = False
        self._waiting: list[tuple[int, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()
        self.quota = quota or QuotaState()

    # --- scheduling -------------------------------------------------------

    @asynccontextmanager
    async def _slot(self, priority: Priority):
        if self._busy:
            fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            heapq.heappush(self._waiting, (priority, next(self._seq), fut))
            try:
                await fut
            except asyncio.CancelledError:
                if fut.done() and not fut.cancelled():
                    self._release()  # slot was handed to us just as we were cancelled
                raise
        else:
            self._busy = True
        try:
            yield
        finally:
            self._release()

    def _release(self) -> None:
        while self._waiting:
            _, _, fut = heapq.heappop(self._waiting)
            if not fut.done():
                fut.set_result(None)
                return
        self._busy = False

    # --- quota ------------------------------------------------------------

    def paused_until(self, priority: Priority) -> float | None:
        if self.quota.paused_from is None or time.time() >= self.quota.paused_until:
            return None
        return self.quota.paused_until if priority >= self.quota.paused_from else None

    def _on_rate_limit(self, info: RateLimitInfo) -> None:
        self.quota.last_rate_limit = info
        resets_at = float(info.resets_at or time.time() + 3600)
        if info.status == "rejected":
            # Out of quota: only chat may still try (the user then sees the error).
            self._pause(Priority.URGENT, resets_at)
        elif info.status == "allowed_warning":
            # Claude Code itself warns that the window is running low: leave the rest to the user.
            self._pause(Priority.RESEARCH, resets_at)
        log.info("rate limit: %s %s util=%s", info.status, info.rate_limit_type, info.utilization)

    def _pause(self, from_priority: Priority, until: float) -> None:
        if time.time() >= self.quota.paused_until:
            self.quota.paused_from = None  # previous pause has expired
        if self.quota.paused_from is None or from_priority < self.quota.paused_from:
            self.quota.paused_from = from_priority
        self.quota.paused_until = max(self.quota.paused_until, until)
        log.warning("pausing priority >= %s until %s", from_priority.name, time.ctime(until))

    # --- running ----------------------------------------------------------

    async def run(self, task: ClaudeTask) -> RunResult:
        if (until := self.paused_until(task.priority)) is not None:
            raise QuotaPaused(until)

        async with self._slot(task.priority):
            # The pause may have started while we were waiting for the slot.
            if (until := self.paused_until(task.priority)) is not None:
                raise QuotaPaused(until)
            started = time.monotonic()
            try:
                result = await self._runner(task, self.workspace, self._on_rate_limit)
            except Exception as e:  # CLI missing, process crash, etc.
                log.exception("claude run failed (%s)", task.kind)
                result = RunResult(text="", is_error=True, error=f"{type(e).__name__}: {e}")
            if result.duration_ms is None:
                result.duration_ms = int((time.monotonic() - started) * 1000)

        await self._log(task, result)
        return result

    async def _log(self, task: ClaudeTask, r: RunResult) -> None:
        rl = self.quota.last_rate_limit
        await self.db.execute(
            "INSERT INTO claude_runs (kind, model, session_id, duration_ms, cost_usd, is_error, error, rate_limit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.kind,
                task.model,
                r.session_id,
                r.duration_ms,
                r.cost_usd,
                int(r.is_error),
                r.error,
                json.dumps({"status": rl.status, "type": rl.rate_limit_type, "utilization": rl.utilization})
                if rl
                else None,
            ),
        )

    @property
    def last_rate_limit(self) -> RateLimitInfo | None:
        return self.quota.last_rate_limit

    def status(self) -> dict[str, Any]:
        rl = self.quota.last_rate_limit
        return {
            "busy": self._busy,
            "waiting": len(self._waiting),
            "paused_from": self.quota.paused_from.name if self.paused_until(Priority.BACKGROUND) else None,
            "paused_until": time.ctime(self.quota.paused_until) if self.paused_until(Priority.BACKGROUND) else None,
            "rate_limit": None if rl is None else {"status": rl.status, "type": rl.rate_limit_type, "utilization": rl.utilization},
        }
