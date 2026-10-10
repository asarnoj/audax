"""SQLite storage. Schema changes are appended to MIGRATIONS and applied in order."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import aiosqlite

# Never edit an existing entry; add a new one instead.
MIGRATIONS: list[str] = [
    # 1: initial schema (see docs/PLAN.md §10 and docs/ATTENTION.md §9)
    """
    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        kind TEXT NOT NULL,
        dedupe_key TEXT,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (source, dedupe_key)
    );

    -- The outbox. features = value features from triage (ATTENTION.md §4.1).
    CREATE TABLE candidates (
        id INTEGER PRIMARY KEY,
        event_id INTEGER REFERENCES events(id),
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        url TEXT,
        features TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending',   -- pending|sent|shelved|dropped|expired
        tier TEXT,                                -- interrupt|next_gap|bundle|digest|shelf|drop
        expires_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- Decision trace: every dispatcher decision with inputs, choice and module versions.
    CREATE TABLE decisions (
        id INTEGER PRIMARY KEY,
        candidate_id INTEGER REFERENCES candidates(id),
        module TEXT NOT NULL,            -- e.g. dispatcher@1
        tier TEXT NOT NULL,
        reason TEXT NOT NULL,
        context TEXT NOT NULL DEFAULT '{}',
        shadow INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        telegram_message_id INTEGER NOT NULL,
        direction TEXT NOT NULL,         -- out|in
        candidate_id INTEGER REFERENCES candidates(id),
        text TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE feedback (
        id INTEGER PRIMARY KEY,
        message_id INTEGER REFERENCES messages(id),
        candidate_id INTEGER REFERENCES candidates(id),
        signal TEXT NOT NULL,            -- reaction|reply|text|poll|command|no_response
        value TEXT NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- Manager hypotheses (ATTENTION.md §4.6).
    CREATE TABLE experiments (
        id INTEGER PRIMARY KEY,
        area TEXT NOT NULL,              -- pacing|timing|content|tone
        status TEXT NOT NULL DEFAULT 'active',  -- active|kept|adjusted|reverted
        hypothesis TEXT NOT NULL,
        outcome TEXT,
        evaluate_after TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        closed_at TEXT
    );

    CREATE TABLE research_runs (
        id INTEGER PRIMARY KEY,
        research_id TEXT NOT NULL,
        findings INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE claude_runs (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        model TEXT,
        session_id TEXT,
        duration_ms INTEGER,
        cost_usd REAL,
        is_error INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        rate_limit TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    # 2: calendars (M2)
    """
    -- One row per event instance. key = uid + recurrence id, stable when an instance moves.
    CREATE TABLE calendar_events (
        calendar TEXT NOT NULL,
        key TEXT NOT NULL,
        series_key TEXT NOT NULL,
        summary TEXT NOT NULL,
        location TEXT,
        description TEXT,
        start TEXT NOT NULL,             -- ISO 8601 with offset
        end TEXT NOT NULL,
        all_day INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',  -- active|cancelled|removed
        first_seen TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (calendar, key)
    );
    CREATE INDEX calendar_events_start ON calendar_events(start);

    -- Recurring kinds of sessions (course + activity) and whether they apply to användaren.
    CREATE TABLE calendar_series (
        id INTEGER PRIMARY KEY,
        calendar TEXT NOT NULL,
        series_key TEXT NOT NULL,
        label TEXT NOT NULL,
        relevance TEXT NOT NULL DEFAULT 'unknown',  -- unknown|mine|not_mine|sometimes
        note TEXT,
        asked_at TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (calendar, series_key)
    );

    CREATE TABLE polls (
        poll_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        message_id INTEGER REFERENCES messages(id),
        answer TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        answered_at TEXT
    );
    """,
    # 3: research + triage + dispatcher (M3)
    """
    ALTER TABLE events ADD COLUMN triaged_at TEXT;
    -- A sent message can carry several candidates (a bundle).
    CREATE TABLE message_candidates (
        message_id INTEGER NOT NULL REFERENCES messages(id),
        candidate_id INTEGER NOT NULL REFERENCES candidates(id),
        PRIMARY KEY (message_id, candidate_id)
    );
    """,
    # 4: who sent an outgoing message: chat (reply in the conversation) | proactive | command
    """
    ALTER TABLE messages ADD COLUMN origin TEXT;
    UPDATE messages SET origin = CASE
        WHEN direction = 'in' THEN NULL
        WHEN candidate_id IS NOT NULL
          OR id IN (SELECT message_id FROM message_candidates)
          OR id IN (SELECT message_id FROM polls WHERE message_id IS NOT NULL) THEN 'proactive'
        ELSE 'chat' END;
    """,
    # 5: Manager (M4): proposals the owner approves, and calibration check-ins
    """
    CREATE TABLE proposals (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,              -- add_research|remove_research|other
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending',  -- pending|asked|accepted|rejected|expired
        message_id INTEGER REFERENCES messages(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        decided_at TEXT
    );
    """,
    # 6: build mode: changes to the agent itself, built on a branch and approved by användaren
    """
    CREATE TABLE changes (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        request TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',  -- queued|building|ready|failed|deployed|discarded
        summary TEXT,
        risk TEXT,
        diffstat TEXT,
        test_output TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        decided_at TEXT
    );
    """,
    # 7: scout - a portfolio of search queries per research mission, with measured yield
    """
    CREATE TABLE scout_queries (
        id INTEGER PRIMARY KEY,
        mission TEXT NOT NULL,
        query TEXT NOT NULL,
        rationale TEXT,
        status TEXT NOT NULL DEFAULT 'active',  -- active|retired
        runs INTEGER NOT NULL DEFAULT 0,
        last_run TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (mission, query)
    );
    """,
    # 8: one voice (docs/ARCHITECTURE.md): what the chat session wrote itself, requests from
    # Director/Manager, and notes routed to them with the owner's exact words
    """
    ALTER TABLE messages ADD COLUMN in_session INTEGER NOT NULL DEFAULT 0;
    UPDATE messages SET in_session = 1 WHERE origin = 'chat';

    CREATE TABLE requests (
        id INTEGER PRIMARY KEY,
        owner TEXT NOT NULL,             -- director|manager
        kind TEXT NOT NULL,              -- question|need
        what TEXT NOT NULL,              -- what to ask/say
        why TEXT NOT NULL,               -- why the owner wants it (context for phrasing)
        status TEXT NOT NULL DEFAULT 'pending',  -- pending|asked|answered|expired
        message_id INTEGER REFERENCES messages(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        asked_at TEXT,
        answered_at TEXT
    );

    CREATE TABLE notes (
        id INTEGER PRIMARY KEY,
        owner TEXT NOT NULL,             -- director|manager
        verbatim TEXT NOT NULL,          -- the owner's exact words
        interpretation TEXT NOT NULL,    -- the chat's reading of them
        request_id INTEGER REFERENCES requests(id),
        message_id INTEGER REFERENCES messages(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        processed_at TEXT
    );
    """,
    # 9: web layer - which way of reading works per domain (agent/web.py)
    """
    CREATE TABLE web_domains (
        domain TEXT NOT NULL,
        method TEXT NOT NULL,            -- api|direct|reader
        ok INTEGER NOT NULL DEFAULT 0,
        fail INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (domain, method)
    );
    """,
    # 10: observability - what each Claude run actually did
    """
    ALTER TABLE claude_runs ADD COLUMN trace TEXT;     -- JSON: tool calls (name, input, error)
    ALTER TABLE claude_runs ADD COLUMN output TEXT;    -- start of the final answer
    ALTER TABLE research_runs ADD COLUMN returned INTEGER;  -- findings returned (new + already known)
    ALTER TABLE research_runs ADD COLUMN skipped TEXT;       -- JSON: what was found but left out, and why
    """,
]


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not opened")
        return self._conn

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _migrate(self) -> None:
        (version,) = await (await self.conn.execute("PRAGMA user_version")).fetchone()
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            await self.conn.executescript(sql)
            await self.conn.execute(f"PRAGMA user_version = {i}")
        await self.conn.commit()

    async def execute(self, sql: str, params: tuple = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        return list(await (await self.conn.execute(sql, params)).fetchall())

    async def fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        return await (await self.conn.execute(sql, params)).fetchone()

    async def backup(self, directory: Path, keep: int = 7) -> Path:
        """Consistent online backup (includes WAL) to data/backups, keeping the newest `keep` files."""
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"agent-{date.today():%Y-%m-%d}.db"
        tmp = target.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        await self.conn.execute("VACUUM INTO ?", (str(tmp),))
        tmp.replace(target)
        for old in sorted(directory.glob("agent-*.db"))[:-keep]:
            old.unlink()
        return target

    async def kv_get(self, key: str, default: Any = None) -> Any:
        row = await self.fetchone("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    async def kv_set(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
            (key, json.dumps(value)),
        )
