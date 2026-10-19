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
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from agent import trail
from agent.db import Database
from agent.vocab import OUT_TOOLS, role_of

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
    trace: list[dict] = field(default_factory=list)  # filled live while the task runs (the office page shows it)


@dataclass
class RunResult:
    text: str  # everything the model wrote in this run, in order (text before and between tool calls included)
    final_text: str = ""  # only what it wrote after its last tool call
    session_id: str | None = None
    structured: Any = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    is_error: bool = False
    error: str | None = None
    trace: list[dict] = field(default_factory=list)  # tool calls: {"tool", "input", "error"}
    context_tokens: int | None = None  # largest single model call (input + cached), i.e. how much context is resent
    usage: dict[str, int] = field(default_factory=dict)  # totals: input, output, cache_read, cache_write, turns
    run_id: int | None = None  # this run's row in claude_runs (#R)


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
        # WebFetch is replaced by web_read; removing it entirely stops Claude reaching for it and misreading the denial.
        disallowed_tools=[*task.disallowed_tools, "WebFetch"],
        # Non-interactive: anything not explicitly allowed is denied, never prompted.
        permission_mode="dontAsk",
        # Load workspace/CLAUDE.md and workspace/.claude/settings.json only.
        setting_sources=["project"],
        # Only the MCP servers given here: the CLI is logged in to the owner's claude.ai account, whose connectors
        # (Gmail with send and trash, Drive, Calendar) would otherwise be loaded into every run.
        strict_mcp_config=True,
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
    trace = task.trace
    went_out = False
    by_id: dict[str, dict] = {}
    context = 0
    result: ResultMessage | None = None
    async for msg in query(prompt=task.prompt, options=options):
        if isinstance(msg, AssistantMessage):
            texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
            if msg.usage:
                u = msg.usage
                context = max(context, sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
            for b in msg.content:
                if isinstance(b, ToolUseBlock) and b.name != "ToolSearch":
                    entry = {"tool": b.name.removeprefix("mcp__"), "input": _short(b.input), "error": False}
                    trace.append(entry)
                    if not went_out and entry["tool"].startswith(OUT_TOOLS):   # out of the office: on the office page too
                        went_out = True
                        await trail.emit("out", role_of(task.kind), task=task.kind)
                    by_id[b.id] = entry
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock) and b.tool_use_id in by_id and b.is_error:
                    by_id[b.tool_use_id]["error"] = True
        elif isinstance(msg, RateLimitEvent):
            on_rate_limit(msg.rate_limit_info)
        elif isinstance(msg, ResultMessage):
            result = msg

    if result is None:
        return RunResult(text="", is_error=True, error="no result message", trace=trace)
    u = result.usage or {}
    return RunResult(
        trace=trace,
        context_tokens=context or None,
        usage={
            "input": u.get("input_tokens") or 0,
            "output": u.get("output_tokens") or 0,
            "cache_read": u.get("cache_read_input_tokens") or 0,
            "cache_write": u.get("cache_creation_input_tokens") or 0,
            "turns": result.num_turns or 0,
        },
        # All of it: a reply written before a tool call (e.g. listing findings, then marking them as told) is part of
        # the answer. Taking only the last block once sent the owner just a closing line (13 Oct).
        text="\n\n".join(t.strip() for t in texts if t.strip()) or (result.result or ""),
        final_text=result.result or (texts[-1] if texts else ""),
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
    # The heavy, optional work (search missions, deep sweeps, reflection) can pause on its own, while the cheap
    # pipeline (triage, dispatcher, page reading) keeps the senses working.
    heavy_paused_until: float = 0.0
    last_rate_limit: RateLimitInfo | None = None


# The runs in progress, in every gate, for the office page (agent/office.py): id -> (task, started).
RUNNING: dict[int, tuple[ClaudeTask, float]] = {}

HEAVY_KINDS = {"research", "research_deep", "director_reflect", "researcher_plan"}
WEEK_STOP = 0.9  # mechanical quota guard: above this share of the weekly window, all background work waits


def _short(value: Any, limit: int = 160) -> str:
    """Tool input for the trace: the interesting part (query, url, path), not whole documents."""
    if isinstance(value, dict):
        for key in ("query", "url", "path", "file_path", "pattern", "repo", "action", "mode", "minutes"):
            if key in value:
                rest = {k: v for k, v in value.items() if k in ("repo", "path", "action") and k != key}
                return (str(value[key]) + (f" {rest}" if rest else ""))[:limit]
        return json.dumps(value, ensure_ascii=False)[:limit]
    return str(value)[:limit]


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
            weekly = (info.rate_limit_type or "").startswith("seven_day")
            if weekly and (info.utilization or 0) < WEEK_STOP:
                # The weekly window is filling but has room, and it is shared with the owner's own Claude use:
                # hold the heavy optional work until it resets, keep the cheap pipeline (senses, triage) running.
                self.quota.heavy_paused_until = max(self.quota.heavy_paused_until, resets_at)
                log.warning("weekly window at %s: heavy work waits until %s", info.utilization, time.ctime(resets_at))
            else:
                # The 5-hour window is nearly full (or the week is): leave the rest to the owner.
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
        if task.kind in HEAVY_KINDS and time.time() < self.quota.heavy_paused_until:
            raise QuotaPaused(self.quota.heavy_paused_until)

        async with self._slot(task.priority):
            # The pause may have started while we were waiting for the slot.
            if (until := self.paused_until(task.priority)) is not None:
                raise QuotaPaused(until)
            started = time.monotonic()
            # The run's row exists from the start, so what it writes carries its id (agent/trail.py). The id stays
            # set after the run, for the caller applying what it returned.
            run_id = await self.db.execute("INSERT INTO claude_runs (kind, model) VALUES (?, ?)", (task.kind, task.model))
            trail.current_run.set(run_id)
            await trail.emit("start", role_of(task.kind), task=task.kind)
            RUNNING[id(task)] = (task, time.time())
            try:
                result = await self._runner(task, self.workspace, self._on_rate_limit)
            except Exception as e:  # CLI missing, process crash, etc.
                log.exception("claude run failed (%s)", task.kind)
                result = RunResult(text="", is_error=True, error=f"{type(e).__name__}: {e}")
            finally:
                RUNNING.pop(id(task), None)
            if result.duration_ms is None:
                result.duration_ms = int((time.monotonic() - started) * 1000)

        result.run_id = run_id
        await self._log(task, result)
        await trail.emit("end", role_of(task.kind), task=task.kind, failed=result.is_error)
        return result

    async def _log(self, task: ClaudeTask, r: RunResult) -> None:
        rl = self.quota.last_rate_limit
        # A resumed session reports its running total; this run's own cost is the increase since the last run.
        own = r.cost_usd
        if r.session_id and r.cost_usd is not None:
            prev = await self.db.fetchone(
                "SELECT session_cost_usd FROM claude_runs WHERE session_id = ? AND session_cost_usd IS NOT NULL ORDER BY id DESC LIMIT 1",
                (r.session_id,),
            )
            if prev and prev["session_cost_usd"] is not None and r.cost_usd >= prev["session_cost_usd"]:
                own = r.cost_usd - prev["session_cost_usd"]
        await self.db.execute(
            "UPDATE claude_runs SET session_id = ?, duration_ms = ?, cost_usd = ?, session_cost_usd = ?, is_error = ?, error = ?, rate_limit = ?, "
            "trace = ?, output = ?, context_tokens = ?, input_tokens = ?, output_tokens = ?, cache_read_tokens = ?, cache_write_tokens = ?, turns = ?, "
            "finished_at = datetime('now') WHERE id = ?",
            (
                r.session_id,
                r.duration_ms,
                own,
                r.cost_usd if r.session_id else None,
                int(r.is_error),
                r.error,
                json.dumps({"status": rl.status, "type": rl.rate_limit_type, "utilization": rl.utilization, "resets_at": rl.resets_at})
                if rl
                else None,
                json.dumps(r.trace, ensure_ascii=False) if r.trace else None,
                (r.text or "")[:4000] or None,
                r.context_tokens,
                r.usage.get("input"),
                r.usage.get("output"),
                r.usage.get("cache_read"),
                r.usage.get("cache_write"),
                r.usage.get("turns"),
                r.run_id,
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
