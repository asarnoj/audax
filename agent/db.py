"""SQLite storage. Schema changes are appended to MIGRATIONS and applied in order."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import aiosqlite

# Never edit an existing entry; add a new one instead.
MIGRATIONS: list[str] = [
    # 1: initial schema (see docs/archive/PLAN.md §10 and docs/archive/ATTENTION.md §9)
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
    # 8: one voice (docs/MODEL.md): what the chat session wrote itself, requests from
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
    # 11: per-sense pace and yield, so the Director can tune how often each one is checked
    """
    CREATE TABLE stream_stats (
        stream TEXT PRIMARY KEY,
        polls INTEGER NOT NULL DEFAULT 0,
        fetched INTEGER NOT NULL DEFAULT 0,   -- items seen (before prefilter and dedupe)
        new INTEGER NOT NULL DEFAULT 0,       -- new events created
        errors INTEGER NOT NULL DEFAULT 0,
        first_poll TEXT NOT NULL DEFAULT (datetime('now')),
        last_poll TEXT,
        last_new TEXT,
        last_error TEXT
    );
    """,
    # 12: how big each run's context was (tokens sent per model call, the main driver of quota use)
    """
    ALTER TABLE claude_runs ADD COLUMN context_tokens INTEGER;
    """,
    # 13: full token accounting per run, for usage analysis (audax usage)
    """
    ALTER TABLE claude_runs ADD COLUMN input_tokens INTEGER;
    ALTER TABLE claude_runs ADD COLUMN output_tokens INTEGER;
    ALTER TABLE claude_runs ADD COLUMN cache_read_tokens INTEGER;
    ALTER TABLE claude_runs ADD COLUMN cache_write_tokens INTEGER;
    ALTER TABLE claude_runs ADD COLUMN turns INTEGER;
    """,
    # 14: one candidate per event (two concurrent triage runs made duplicates, 12 Oct; the later ones lose
    # their event link, they were all dropped), and problems the chat noticed, for the maintainer.
    """
    UPDATE candidates SET event_id = NULL
    WHERE event_id IS NOT NULL AND id NOT IN (SELECT min(id) FROM candidates WHERE event_id IS NOT NULL GROUP BY event_id);
    CREATE UNIQUE INDEX candidates_one_per_event ON candidates(event_id) WHERE event_id IS NOT NULL;
    CREATE TABLE problems (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        description TEXT NOT NULL,
        resolved_at TEXT
    );
    """,
    # 15: the search map (docs/SENSES.md, "Täckning"): leads that searches come across, so the net grows over
    # time, and coverage audits against an independent benchmark, so Audax can see what it misses.
    """
    CREATE TABLE leads (
        id INTEGER PRIMARY KEY,
        mission TEXT NOT NULL,
        key TEXT NOT NULL,
        name TEXT NOT NULL,
        url TEXT,
        kind TEXT,
        why TEXT,
        times INTEGER NOT NULL DEFAULT 1,
        first_seen TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen TEXT NOT NULL DEFAULT (datetime('now')),
        status TEXT NOT NULL DEFAULT 'new',
        reason TEXT,
        UNIQUE (mission, key)
    );
    CREATE TABLE coverage (
        id INTEGER PRIMARY KEY,
        mission TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        benchmark TEXT NOT NULL,
        covered TEXT NOT NULL,
        missing TEXT NOT NULL,
        recall REAL,
        note TEXT
    );
    """,
    # 16: Claude Code reports a resumed session's cost as a running total for the whole session, so every chat
    # reply was logged with all earlier replies' cost (e.g. 2.22, 2.25, 2.28 ...). Keep the running total in its
    # own column and make cost_usd the run's own cost, also for the history.
    """
    ALTER TABLE claude_runs ADD COLUMN session_cost_usd REAL;
    UPDATE claude_runs SET session_cost_usd = cost_usd WHERE session_id IS NOT NULL;
    WITH d AS (
        SELECT id, cost_usd - LAG(cost_usd) OVER (PARTITION BY session_id ORDER BY id) AS own
        FROM claude_runs WHERE session_id IS NOT NULL AND cost_usd IS NOT NULL
    )
    UPDATE claude_runs SET cost_usd = (SELECT own FROM d WHERE d.id = claude_runs.id)
    WHERE id IN (SELECT id FROM d WHERE own IS NOT NULL AND own >= 0);
    """,
    # 17: pursuits (docs/archive/ATTENTION.md, "Ansökningsspåret"): everything the owner has shown interest in, applied
    # to or turned down, with a deadline and a next step, so Audax can nudge at the right time and follow up.
    """
    CREATE TABLE pursuits (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        organization TEXT,
        url TEXT,
        candidate_id INTEGER REFERENCES candidates(id),
        status TEXT NOT NULL DEFAULT 'interested',
        deadline TEXT,
        remind_at TEXT,
        next_step TEXT,
        note TEXT,
        source TEXT,
        nudged_at TEXT,
        nudge_candidate_id INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE pursuit_log (
        id INTEGER PRIMARY KEY,
        pursuit_id INTEGER NOT NULL REFERENCES pursuits(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        status TEXT,
        note TEXT,
        by TEXT
    );
    """,
    # 18: the internal mail (docs/MODEL.md, "Postgången") replaces notes and requests, and messages link to the
    # things they are about (posts, pursuits, findings ...) so the Assistant can look them up instead of remembering.
    """
    CREATE TABLE posts (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        sender TEXT NOT NULL,            -- owner|assistant|director|researcher|manager|system
        recipient TEXT NOT NULL,         -- assistant|director|researcher|manager
        kind TEXT NOT NULL,              -- note|question|answer|order|feedback|report
        body TEXT NOT NULL,              -- the content, or the sender's reading of the owner's words
        verbatim TEXT,                   -- the owner's exact words, when they matter
        context TEXT,                    -- why (for questions: not to be said, only to phrase it well)
        answer_to INTEGER REFERENCES posts(id),
        from_owner INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'new',   -- new|asked|done|expired
        message_id INTEGER REFERENCES messages(id),
        asked_at TEXT,
        handled_at TEXT,
        outcome TEXT
    );
    CREATE INDEX posts_inbox ON posts(recipient, status);
    INSERT INTO posts (created_at, sender, recipient, kind, body, context, status, asked_at)
        SELECT created_at, owner, 'assistant', 'question', what, why, CASE status WHEN 'asked' THEN 'asked' ELSE 'new' END, asked_at
        FROM requests WHERE status IN ('pending', 'asked');
    INSERT INTO posts (created_at, sender, recipient, kind, body, verbatim, from_owner)
        SELECT created_at, 'assistant', owner, 'note', interpretation, verbatim, 1 FROM notes WHERE processed_at IS NULL;
    CREATE TABLE message_refs (
        message_id INTEGER NOT NULL REFERENCES messages(id),
        ref TEXT NOT NULL,               -- e.g. #P12 (post), #S3 (pursuit), #F210 (finding)
        PRIMARY KEY (message_id, ref)
    );
    """,
    # 19: the schedule (docs/MODEL.md, "Schemat"): findings get a delivery time instead of waiting in a queue that
    # is re-sorted every ten minutes, and reminders and promises are bookings with a time.
    """
    ALTER TABLE candidates ADD COLUMN deliver_at TEXT;
    CREATE TABLE bookings (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        kind TEXT NOT NULL,              -- reminder (the owner asked) | promise (the Assistant said it would)
        title TEXT NOT NULL,
        body TEXT NOT NULL,              -- what to say, as a brief for the Assistant
        due_at TEXT NOT NULL,            -- UTC ISO
        latest_at TEXT,                  -- a promise may move, but not past this
        fixed INTEGER NOT NULL DEFAULT 0,-- the owner's own booking: only the owner moves it
        requested_by TEXT NOT NULL,
        ref TEXT,                        -- what it is about, e.g. #S3
        status TEXT NOT NULL DEFAULT 'planned',  -- planned|sent|cancelled
        reason TEXT,
        message_id INTEGER REFERENCES messages(id),
        sent_at TEXT
    );
    CREATE INDEX bookings_due ON bookings(status, due_at);
    """,
    # 20: who did what (agent/trail.py): a run's row exists from its start, so the rows it writes carry its id, and
    # the office's event log records what happens in structured fields where it happens.
    """
    ALTER TABLE claude_runs ADD COLUMN finished_at TEXT;
    UPDATE claude_runs SET finished_at = created_at;
    ALTER TABLE posts ADD COLUMN run_id INTEGER;
    ALTER TABLE decisions ADD COLUMN run_id INTEGER;
    ALTER TABLE candidates ADD COLUMN run_id INTEGER;
    ALTER TABLE messages ADD COLUMN run_id INTEGER;
    ALTER TABLE bookings ADD COLUMN run_id INTEGER;
    ALTER TABLE events ADD COLUMN run_id INTEGER;
    ALTER TABLE pursuit_log ADD COLUMN run_id INTEGER;
    ALTER TABLE feedback ADD COLUMN run_id INTEGER;
    CREATE INDEX posts_run ON posts(run_id);
    CREATE INDEX decisions_run ON decisions(run_id);
    CREATE INDEX candidates_run ON candidates(run_id);
    CREATE TABLE office_events (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        run_id INTEGER,
        kind TEXT NOT NULL,             -- start|end|print|pass|drop|place|post|message|deliver|book|pull|save
        role TEXT,
        data TEXT NOT NULL DEFAULT '{}'
    );
    """,
    # 21: one kind of record for what the owner goes for (agent/pursuits.py): goals, projects, opportunities and
    # Director's ideas are all #S, an opportunity or idea under the goal it serves. And one account per run, on the
    # run, instead of the logs and the journal in memory.
    """
    ALTER TABLE pursuits ADD COLUMN kind TEXT NOT NULL DEFAULT 'pursuit';   -- goal|project|pursuit|idea
    ALTER TABLE pursuits ADD COLUMN parent_id INTEGER REFERENCES pursuits(id);
    ALTER TABLE pursuits ADD COLUMN why TEXT;
    ALTER TABLE pursuits ADD COLUMN about TEXT;
    ALTER TABLE pursuits ADD COLUMN run_id INTEGER;
    ALTER TABLE claude_runs ADD COLUMN summary TEXT;
    """,
    # 22: the office's items (agent/forms.py, agent/items.py): one core for every form, one history, one set of links.
    # The #S records move in with their numbers and history; their own tables go.
    """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY,
        form TEXT NOT NULL,                 -- aim|step|experiment|belief|question|rule|routine
        kind TEXT,
        title TEXT NOT NULL,
        status TEXT NOT NULL,
        owner TEXT,                         -- the role that owns it
        parent_id INTEGER REFERENCES items(id),
        source TEXT,                        -- JSON: {"type": owner|observed|inferred|external, ...}
        review_at TEXT,
        valid_until TEXT,
        data TEXT NOT NULL DEFAULT '{}',    -- the form's own fields
        by TEXT,
        run_id INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX items_form ON items(form, status);
    CREATE INDEX items_parent ON items(parent_id);
    CREATE INDEX items_review ON items(review_at);
    CREATE TABLE item_log (
        id INTEGER PRIMARY KEY,
        item_id INTEGER NOT NULL REFERENCES items(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        by TEXT,
        run_id INTEGER,
        status TEXT,
        changes TEXT,
        note TEXT
    );
    CREATE INDEX item_log_item ON item_log(item_id);
    CREATE TABLE item_links (
        from_id INTEGER NOT NULL REFERENCES items(id),
        to_id INTEGER NOT NULL REFERENCES items(id),
        relation TEXT NOT NULL,
        by TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (from_id, to_id, relation)
    );
    INSERT INTO items (id, form, kind, title, status, owner, parent_id, source, data, by, run_id, created_at, updated_at)
    SELECT id, 'aim', kind, title, status, 'director', parent_id,
           json_object('type', CASE WHEN source IN ('chat', 'reaction') THEN 'owner' ELSE 'inferred' END, 'by', source),
           json_patch('{}', json_object('why', why, 'about', about, 'next_step', next_step, 'deadline', deadline,
                      'remind_at', remind_at, 'organization', organization, 'url', url, 'candidate_id', candidate_id,
                      'nudged_at', nudged_at, 'nudge_candidate_id', nudge_candidate_id)),
           source, run_id, created_at, updated_at
    FROM pursuits;
    INSERT INTO item_log (item_id, created_at, by, run_id, status, note)
    SELECT pursuit_id, created_at, by, run_id, status, note FROM pursuit_log ORDER BY id;
    DROP TABLE pursuit_log;
    DROP TABLE pursuits;
    """,
    # 23: search (agent/search.py): the conversation, the posts, the findings and the items in one full-text index
    # that tolerates inflection (trigrams: "track" finds "trackval"), kept current by triggers.
    """
    CREATE VIRTUAL TABLE search USING fts5(body, kind UNINDEXED, ref UNINDEXED, who UNINDEXED, at UNINDEXED, tokenize='trigram');
    INSERT INTO search (body, kind, ref, who, at)
        SELECT text, 'message', id, CASE direction WHEN 'in' THEN 'owner' ELSE 'assistant' END, created_at FROM messages;
    INSERT INTO search (body, kind, ref, who, at)
        SELECT body || coalesce(char(10) || verbatim, ''), 'post', id, sender, created_at FROM posts;
    INSERT INTO search (body, kind, ref, who, at)
        SELECT title || char(10) || coalesce(body, '') || coalesce(char(10) || url, ''), 'finding', id, 'office', created_at FROM candidates;
    INSERT INTO search (body, kind, ref, who, at)
        SELECT title || char(10) || data, 'item', id, coalesce(owner, ''), created_at FROM items;
    CREATE TRIGGER search_messages AFTER INSERT ON messages BEGIN
        INSERT INTO search (body, kind, ref, who, at)
        VALUES (new.text, 'message', new.id, CASE new.direction WHEN 'in' THEN 'owner' ELSE 'assistant' END, new.created_at);
    END;
    CREATE TRIGGER search_posts AFTER INSERT ON posts BEGIN
        INSERT INTO search (body, kind, ref, who, at) VALUES (new.body || coalesce(char(10) || new.verbatim, ''), 'post', new.id, new.sender, new.created_at);
    END;
    CREATE TRIGGER search_candidates AFTER INSERT ON candidates BEGIN
        INSERT INTO search (body, kind, ref, who, at)
        VALUES (new.title || char(10) || coalesce(new.body, '') || coalesce(char(10) || new.url, ''), 'finding', new.id, 'office', new.created_at);
    END;
    CREATE TRIGGER search_items AFTER INSERT ON items BEGIN
        INSERT INTO search (body, kind, ref, who, at) VALUES (new.title || char(10) || new.data, 'item', new.id, coalesce(new.owner, ''), new.created_at);
    END;
    CREATE TRIGGER search_items_update AFTER UPDATE OF title, data ON items BEGIN
        DELETE FROM search WHERE kind = 'item' AND ref = old.id;
        INSERT INTO search (body, kind, ref, who, at) VALUES (new.title || char(10) || new.data, 'item', new.id, coalesce(new.owner, ''), new.created_at);
    END;
    """,
    # 24: a run's whole generation, to read afterwards on its page: the prompt it was given, and in order what it
    # thought, which tools it called and what came back (24 Oct: "kan jag se hela genereringen?").
    """
    ALTER TABLE claude_runs ADD COLUMN prompt TEXT;
    ALTER TABLE claude_runs ADD COLUMN transcript TEXT;
    """,
    # 25: standing orders (agent/posts.py): an order holds until its date or until its sender withdraws it, and both
    # sides see it in their review (24 Oct: an order to freeze the search held for weeks, unseen by its sender).
    """
    ALTER TABLE posts ADD COLUMN until TEXT;
    ALTER TABLE posts ADD COLUMN withdrawn_at TEXT;
    """,
]

# Tables whose rows carry the id of the run that wrote them (agent/trail.py).
STAMPED = {"posts", "decisions", "candidates", "messages", "bookings", "events", "items", "item_log", "feedback"}


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

    async def insert(self, table: str, ignore: bool = False, **values: Any) -> int:
        """Insert a row; in a stamped table it carries the id of the run writing it (agent/trail.py)."""
        from agent.trail import current_run

        if table in STAMPED and "run_id" not in values:
            values["run_id"] = current_run.get()
        cols = ", ".join(values)
        cur = await self.conn.execute(
            f"INSERT {'OR IGNORE ' if ignore else ''}INTO {table} ({cols}) VALUES ({', '.join('?' * len(values))})", tuple(values.values()))
        await self.conn.commit()
        return cur.lastrowid if cur.rowcount else 0

    async def execute(self, sql: str, params: tuple = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid

    async def execute_count(self, sql: str, params: tuple = ()) -> int:
        """Like execute, but returns the number of rows changed (for atomic claims)."""
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.rowcount

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
