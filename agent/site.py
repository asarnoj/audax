"""The office's website, read only, for the owner (docs/KONTORET.md, docs/OPERATIONS.md).

Served inside the engine (so it sees the runs in progress) on localhost; nginx makes it reachable on the Tailscale
address and nowhere else. The data comes from agent/office.py.

- /                    the illustrated office (web/office.html)
- /api/state           the state of right now, with the point in the event log it was read at
- /api/stream          what happens from then on (server-sent events; a reconnecting page carries on from its last id)
- /api/thing/<name>    what a click shows: desk-<role>, archive, bin, calendar, board
- /api/work/<id>       one piece of work (#R)
- /data/               the raw data, in plain pages: the tables, the files, a case by its id, one run
"""

from __future__ import annotations

import asyncio
import html
import re
from contextlib import contextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from agent import refs
from agent.config import ROOT
from agent.db import Database
from agent.forms import FORMS, LETTERS
from agent.items import Items, ref
from agent.office import Office, sse
from agent.vocab import NAMES, role_of, task_name

TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".txt", ".json", ".csv", ".log"}
PAGE = 100  # table rows per page
ID = re.compile(r"#([PFBR" + "".join(LETTERS) + r"])(\d+)\b")  # an id in running text (refs.PATTERN also takes it without the #)


def e(value) -> str:
    return html.escape("" if value is None else str(value))


def linked(text: str | None, limit: int | None = None) -> str:
    """Escaped text where every id (#P12, #S3, #F210, #B4, #R88) links to its page."""
    text = text or ""
    if limit and len(text) > limit:
        text = text[:limit] + "…"

    def link(m: re.Match) -> str:
        if m.group(1) == "R":
            href = f"/data/run/{m.group(2)}"
        elif m.group(1) in LETTERS:
            href = f"/data/item/{m.group(2)}"
        else:
            href = f"/data/case/{m.group(1)}{m.group(2)}"
        return f'<a href="{href}">{m.group(0)}</a>'
    return ID.sub(link, e(text))


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{e(title)} · Audax</title></head><body>"
        '<p><a href="/">Office</a> · <a href="/data/">Data</a> · <a href="/data/items/">Items</a> · <a href="/data/case/">Cases</a> · <a href="/data/files/">Files</a> · '
        '<a href="/data/tables/">Tables</a></p>'
        f"<h1>{e(title)}</h1>{body}</body></html>"
    )


def build_app(db: Database, office: Office, roots: dict[str, Path]) -> Starlette:
    async def office_page(request: Request) -> Response:
        return FileResponse(ROOT / "web" / "office.html")

    # --- the office, live --------------------------------------------------------------------

    async def api_state(request: Request) -> Response:
        return JSONResponse(await office.state())

    async def api_stream(request: Request) -> Response:
        start = request.headers.get("last-event-id") or request.query_params.get("from")
        cursor = int(start) if start and start.isdigit() else await office.cursor()

        async def events():
            nonlocal cursor
            quiet = 0
            while not await request.is_disconnected():
                happened = await office.since(cursor)
                for h in happened:
                    yield sse(h)
                if happened:
                    cursor = happened[-1]["id"]
                    yield sse({"type": "counts", **await office.counts()})   # what the piles show now
                quiet = 0 if happened else quiet + 1
                if quiet >= 15:  # a comment now and then keeps proxies from closing a quiet stream
                    yield ": still here\n\n"
                    quiet = 0
                await asyncio.sleep(1)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def api_thing(request: Request) -> Response:
        d = await office.detail(request.path_params["name"])
        return JSONResponse(d) if d else JSONResponse({"error": "no such thing"}, 404)

    async def api_work(request: Request) -> Response:
        w = await office.work(request.path_params["id"])
        return JSONResponse(w) if w else JSONResponse({"error": "no such work"}, 404)

    # --- the raw data -------------------------------------------------------------------------

    async def data_index(request: Request) -> Response:
        parts = []
        working = office.working()
        parts.append("<h2>Working now</h2>" + ("<ul>" + "".join(f"<li>{e(NAMES[r])}: {e(w['task'])} ({w['seconds']} s)</li>" for r, w in working.items())
                                                + "</ul>" if working else "<p>No one.</p>"))
        if office.status:
            q = office.status()
            rl = q.get("rate_limit") or {}
            line = f"Quota: {e(rl.get('status') or 'unknown')}"
            if rl.get("utilization") is not None:
                line += f", {e(rl.get('type'))} {round(rl['utilization'] * 100)} %"
            if q.get("paused_until"):
                line += f" · background work paused until {e(q['paused_until'])}"
            parts.append(f"<p>{line}</p>")
        runs = await db.fetchall("SELECT id, kind, finished_at, duration_ms, is_error FROM claude_runs ORDER BY id DESC LIMIT 40")
        parts.append("<h2>Latest work</h2><ul>" + "".join(
            f'<li><a href="/data/run/{r["id"]}">#R{r["id"]}</a> {e(office.local(r["finished_at"]))} · {e(NAMES.get(role_of(r["kind"]), ""))} · '
            f'{e(task_name(r["kind"]))} ({(r["duration_ms"] or 0) // 1000} s){" · failed" if r["is_error"] else ""}</li>' for r in runs) + "</ul>")
        return page("Data", "".join(parts))

    async def items_page(request: Request) -> Response:
        """Every item by form: open first, then what is over."""
        items = Items(db)
        want = request.query_params.get("form")
        parts = ["<p>" + " · ".join(f'<a href="?form={n}">{e(f.label)}</a>' for n, f in FORMS.items()) + ' · <a href="?">alla</a></p>']
        for name, form in FORMS.items():
            if want and want != name:
                continue
            closed = ",".join(f"'{c}'" for c in form.closed) or "''"
            rows = await items.find(name, order=f"status IN ({closed}), updated_at DESC", limit=300)
            if not rows:
                continue
            parts.append(f"<h2>{e(form.label)} ({len(rows)})</h2><p>{e(form.help)}</p><ul>" + "".join(
                f'<li><a href="/data/item/{i["id"]}">{e(ref(i))}</a> {e(i["kind"] or "")} · {e(i["status"])} · {e(i["title"])}'
                + (f' · ägare {e(i["owner"])}' if i["owner"] else "") + "</li>" for i in rows) + "</ul>")
        return page("Items", "".join(parts) if len(parts) > 1 else parts[0] + "<p>Inga än.</p>")

    async def item_page(request: Request) -> Response:
        text = await Items(db).describe(request.path_params["id"])
        if text is None:
            return page("Not found", "<p>No such item.</p>")
        return page(text.splitlines()[0], f"<pre>{linked(text)}</pre>")

    async def run_page(request: Request) -> Response:
        r = await office.run(request.path_params["id"])
        if r is None:
            return page("Not found", "<p>No such run.</p>")
        rows = [("Role", e(NAMES.get(r["role"], r["role"]))), ("Task", e(r["task"]) + f" ({e(r['kind'])})"), ("Time", e(r["at"])), ("Model", e(r["model"])),
                ("Length", f"{(r['duration_ms'] or 0) / 1000:.0f} s"), ("Cost", f"{r['cost_usd']:.3f} $" if r["cost_usd"] is not None else "–"),
                ("Tokens", f"in {e(r['input_tokens'])}, out {e(r['output_tokens'])}, cache {e(r['cache_read_tokens'])}/{e(r['cache_write_tokens'])}, "
                           f"context {e(r['context_tokens'])}"),
                ("Turns", e(r["turns"])), ("Quota", e(r["rate_limit"])), ("Error", e(r["error"]) if r["is_error"] else "no")]
        trace = "".join(f"<li>{e(t['tool'])}: {e(t['input'])}{' · failed' if t.get('error') else ''}</li>" for t in r["trace"])
        return page(f"#R{r['id']}",
                    "<table>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows) + "</table>"
                    f"<h2>Tools</h2>{'<ol>' + trace + '</ol>' if trace else '<p>None.</p>'}"
                    f"<h2>Output (the start)</h2><pre>{linked(r['output'] or '')}</pre>")

    async def case_page(request: Request) -> Response:
        ref = request.path_params.get("ref", "")
        q = request.query_params.get("q", "").strip()
        form = '<form action="/data/case/"><input name="q" placeholder="#S3, #F210 or a few words" value="' + e(q) + '"> <button>Search</button></form>'
        if not ref:
            if q and (m := refs.PATTERN.fullmatch(q)):
                return RedirectResponse(f"/data/case/{m.group(1).upper()}{m.group(2)}")
            result = f"<pre>{linked(await refs.lookup(db, q, limit=40))}</pre>" if q else ""
            return page("Cases", form + result)
        c = await office.case(ref)
        if c is None:
            return page(ref, form + "<p>No case with that id.</p>")
        parts = [form, f"<pre>{linked(c['about'])}</pre>"]
        if c["decisions"]:
            parts.append("<h2>Decisions</h2><ul>" + "".join(f"<li>{e(d['at'])} · {e(d['module'])} · {e(d['decision'])}: {e(d['reason'])}</li>"
                                                         for d in c["decisions"]) + "</ul>")
        if c["messages"]:
            parts.append("<h2>Messages</h2><ul>" + "".join(f"<li>{e(m['at'])} · {e(m['from'])}: {linked(m['text'])}</li>" for m in c["messages"]) + "</ul>")
        if c["posts"]:
            parts.append("<h2>Posts</h2><ul>" + "".join(
                f"<li>{e(p['at'])} · {linked(p['ref'])} {e(p['from'])} → {e(p['to'])} ({e(p['kind'])}, {e(p['status'])}): {linked(p['text'])}</li>"
                for p in c["posts"]) + "</ul>")
        if c["bookings"]:
            parts.append("<h2>Bookings</h2><ul>" + "".join(
                f"<li>{linked(b['ref'])} {e(b['kind'])}, {e(b['status'])}, {e(b['due'])}: {e(b['title'])}</li>" for b in c["bookings"]) + "</ul>")
        return page(c["ref"], "".join(parts))

    async def files_page(request: Request) -> Response:
        rel = request.path_params.get("path", "").strip("/")
        if not rel:
            return page("Files", "<ul>" + "".join(f'<li><a href="/data/files/{n}/">{n}/</a></li>' for n in roots) + "</ul>")
        root_name, _, sub = rel.partition("/")
        if root_name not in roots:
            return page("Not found", "<p>No such folder.</p>")
        root = roots[root_name].resolve()
        path = (root / sub).resolve()
        if not path.is_relative_to(root) or any(part.startswith(".") for part in path.relative_to(root).parts) or not path.exists():
            return page("Not found", "<p>No such file.</p>")
        up = rel.rsplit("/", 1)[0] if "/" in rel else ""
        back = f'<p><a href="/data/files/{e(up)}{"/" if up else ""}">↑ {e(up or "Files")}</a></p>'
        if path.is_dir():
            entries = sorted((p for p in path.iterdir() if not p.name.startswith(".")), key=lambda p: (p.is_file(), p.name))
            items = "".join(f'<li><a href="/data/files/{e(rel)}/{e(p.name)}{"/" if p.is_dir() else ""}">{e(p.name)}{"/" if p.is_dir() else ""}</a></li>'
                            for p in entries)
            return page(rel + "/", back + (f"<ul>{items}</ul>" if items else "<p>Empty.</p>"))
        if path.suffix not in TEXT_SUFFIXES:
            return page(rel, back + f"<p>{path.stat().st_size} bytes, not text.</p>")
        return page(rel, back + f"<pre>{linked(path.read_text(errors='replace'))}</pre>")

    async def tables_page(request: Request) -> Response:
        name = request.path_params.get("name")
        tables = [r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        if not name:
            counts = [(t, (await db.fetchone(f'SELECT count(*) AS n FROM "{t}"'))["n"]) for t in tables]
            return page("Tables", "<ul>" + "".join(f'<li><a href="/data/tables/{t}">{t}</a> ({n})</li>' for t, n in counts) + "</ul>")
        if name not in tables:  # only real table names reach the SQL below
            return page("Not found", "<p>No such table.</p>")
        offset = max(0, int(request.query_params.get("page", "0") or 0)) * PAGE
        rows = await db.fetchall(f'SELECT * FROM "{name}" ORDER BY rowid DESC LIMIT ? OFFSET ?', (PAGE, offset))
        total = (await db.fetchone(f'SELECT count(*) AS n FROM "{name}"'))["n"]
        if not rows:
            return page(name, "<p>No rows.</p>")
        cols = rows[0].keys()
        head = "<tr>" + "".join(f"<th>{e(c)}</th>" for c in cols) + "</tr>"
        body = "".join("<tr>" + "".join(f"<td>{linked(str(r[c]) if r[c] is not None else '', 300)}</td>" for c in cols) + "</tr>" for r in rows)
        n = offset // PAGE
        nav = (f'<p>Rows {offset + 1}–{offset + len(rows)} of {total}, newest first. '
               + (f'<a href="?page={n - 1}">← newer</a> ' if n else "")
               + (f'<a href="?page={n + 1}">older →</a>' if offset + len(rows) < total else "") + "</p>")
        return page(name, nav + f'<table border="1">{head}{body}</table>' + nav)

    return Starlette(routes=[
        Route("/", office_page),
        Route("/api/state", api_state),
        Route("/api/stream", api_stream),
        Route("/api/thing/{name}", api_thing),
        Route("/api/work/{id:int}", api_work),
        Route("/data/", data_index),
        Route("/data/run/{id:int}", run_page),
        Route("/data/items/", items_page),
        Route("/data/item/{id:int}", item_page),
        Route("/data/case/", case_page),
        Route("/data/case/{ref}", case_page),
        Route("/data/files/", files_page),
        Route("/data/files/{path:path}", files_page),
        Route("/data/tables/", tables_page),
        Route("/data/tables/{name}", tables_page),
    ])


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield  # the engine handles SIGTERM/SIGINT itself and stops the site with it


def start(app: Starlette, host: str, port: int) -> tuple[asyncio.Task, _Server]:
    server = _Server(uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off", access_log=False))
    return asyncio.create_task(server.serve(), name="site"), server
