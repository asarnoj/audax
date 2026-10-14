"""The office's web page: the raw data, read only, for the owner (docs/MODEL.md).

Served inside the engine (so it sees the runs in progress) on localhost; Tailscale Serve makes it reachable from the
owner's own devices and nowhere else (docs/OPERATIONS.md). Plain pages with no styling on purpose: the illustrated
office comes later and reads the same data from /api/.

- /                 the office: what each role does right now (live) and the latest events
- /roll/<role>      a role's desk: working memory, files, inbox, history
- /korning/<id>     one Claude run: its steps and output
- /arende/<id>      follow a case by its id (#P, #S, #F, #B), or search
- /arkiv/           the file cabinet: memory and the instance config
- /tabeller/        every database table
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
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from agent import refs
from agent.db import Database
from agent.office import NAMES, ROLES, Office

TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".txt", ".json", ".csv", ".log"}
PAGE = 100  # table rows per page
ID = re.compile(r"#([PSFB])(\d+)\b")  # an id in running text (refs.PATTERN also takes it without the #)


def e(value) -> str:
    return html.escape("" if value is None else str(value))


def linked(text: str | None, limit: int | None = None) -> str:
    """Escaped text where every id (#P12, #S3, #F210, #B4) links to its case."""
    text = text or ""
    if limit and len(text) > limit:
        text = text[:limit] + "…"
    return ID.sub(lambda m: f'<a href="/arende/{m.group(1)}{m.group(2)}">{m.group(0)}</a>', e(text))


def who(name: str) -> str:
    return f'<a href="/roll/{name}">{NAMES[name]}</a>' if name in ROLES else e(NAMES.get(name, name))


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{e(title)} · Audax</title></head><body>"
        '<p><a href="/">Kontoret</a> · ' + " · ".join(f'<a href="/roll/{r}">{NAMES[r]}</a>' for r in ROLES)
        + ' · <a href="/arende/">Ärenden</a> · <a href="/arkiv/">Arkivet</a> · <a href="/tabeller/">Tabellerna</a></p>'
        f"<h1>{e(title)}</h1>{body}</body></html>"
    )


def live(element: str, url: str, seconds: int) -> str:
    return (f"<script>setInterval(() => fetch('{url}').then(r => r.text()).then(t => "
            f"document.getElementById('{element}').innerHTML = t).catch(() => {{}}), {seconds * 1000});</script>")


# --- fragments --------------------------------------------------------------------------


def now_html(now: dict) -> str:
    out = [f"<p>Klockan {e(now['at'])}.</p>"]
    for role, r in now["roles"].items():
        name = who(role) if role in ROLES else e(r["name"])
        if r["working"]:
            for w in r["working"]:
                out.append(f"<h3>{name}: arbetar</h3><p>{e(w['task'])}, sedan {e(w['since'])} ({w['seconds']} s)</p>")
                if w["steps"]:
                    out.append("<ul>" + "".join(f"<li>{e(s)}</li>" for s in w["steps"]) + "</ul>")
        elif role in ROLES:
            out.append(f"<h3>{name}: ledig</h3>")
            if last := r.get("last_run"):
                out.append(f'<p>Senast: {e(last["task"])} {e(last["at"])} (<a href="/korning/{last["id"]}">körning {last["id"]}</a>)'
                           + (" · misslyckades" if last["error"] else "") + "</p>")
        if role in ROLES:
            line = f"Inkorg: {r['inbox']} olästa"
            if "questions_for_owner" in r:
                line += f" · frågor till dig som väntar: {r['questions_for_owner']}"
            out.append(f"<p>{line}</p>")
    if q := now.get("quota"):
        rl = q.get("rate_limit") or {}
        line = f"Kvot: {e(rl.get('status') or 'okänd')}"
        if rl.get("utilization") is not None:
            line += f", {e(rl.get('type'))} {round(rl['utilization'] * 100)} %"
        if q.get("paused_until"):
            line += f" · bakgrundsarbetet pausat till {e(q['paused_until'])}"
        out.append(f"<p>{line}</p>")
    if night := now.get("night"):
        out.append(f"<p>Natten {e(night.get('night'))}: klart {e(', '.join(night.get('done', [])) or 'inget')} · försök {e(night.get('attempts'))}</p>")
    return "".join(out)


def timeline_html(events: list[dict]) -> str:
    items = []
    for ev in events:
        if ev["type"] == "message":
            how = " (på eget initiativ)" if ev["origin"] == "proactive" else ""
            refs_ = " " + " ".join(linked(r) for r in ev["refs"]) if ev["refs"] else ""
            items.append(f"<li>{e(ev['at'])} · {who(ev['from'])} → {who(ev['to'])}{how}: {linked(ev['text'], 400)}{refs_}</li>")
        elif ev["type"] == "post":
            items.append(f"<li>{e(ev['at'])} · {linked(ev['ref'])} {who(ev['from'])} → {who(ev['to'])} ({e(ev['kind'])}, {e(ev['status'])}): "
                         f"{linked(ev['text'], 400)}</li>")
        else:
            cost = f", {ev['cost_usd']:.2f} $" if ev["cost_usd"] is not None else ""
            err = f" · fel: {e(ev['error'])}" if ev["error"] else ""
            items.append(f'<li>{e(ev["at"])} · {who(ev["role"])} körde {e(ev["task"])} ({ev["seconds"]} s{cost}) '
                         f'<a href="/korning/{ev["id"]}">körning {ev["id"]}</a>{err}</li>')
    return "<ul>" + "".join(items) + "</ul>" if items else "<p>Inget ännu.</p>"


# --- the app ------------------------------------------------------------------------------


def build_app(db: Database, office: Office, roots: dict[str, Path]) -> Starlette:
    async def office_page(request: Request) -> Response:
        return page("Kontoret", f'<div id="nu">{now_html(await office.now())}</div>{live("nu", "/fragment/nu", 2)}'
                    f'<h2>Senaste händelser</h2><div id="tid">{timeline_html(await office.timeline(60))}</div>'
                    f'{live("tid", "/fragment/tidslinje", 10)}')

    async def now_fragment(request: Request) -> Response:
        return HTMLResponse(now_html(await office.now()))

    async def timeline_fragment(request: Request) -> Response:
        return HTMLResponse(timeline_html(await office.timeline(60)))

    async def role_page(request: Request) -> Response:
        role = request.path_params["role"]
        if role not in ROLES:
            return page("Finns inte", "<p>Ingen sådan roll.</p>")
        r = await office.role(role)
        files = []
        for f in r["files"]:
            if f["folder"]:
                files.append(f'<li><a href="/arkiv/minne/{e(f["path"])}">{e(f["path"])}</a> ({len(f["files"])} filer)</li>')
            elif f["exists"]:
                files.append(f'<li><a href="/arkiv/minne/{e(f["path"])}">{e(f["path"])}</a></li>')
            else:
                files.append(f"<li>{e(f['path'])} (finns inte än)</li>")
        inbox = "".join(f"<li>{e(p['at'])} · {linked(p['ref'])} från {who(p['from'])} ({e(p['kind'])}, {e(p['status'])}): {linked(p['text'])}</li>"
                        for p in r["inbox"])
        return page(r["name"],
                    f"<h2>Arbetsminne</h2><pre>{linked(r['working_memory'] or '(tomt)')}</pre>"
                    f"<h2>Inkorg</h2>{'<ul>' + inbox + '</ul>' if inbox else '<p>Tom.</p>'}"
                    f"<h2>Filer</h2><ul>{''.join(files)}</ul>"
                    f"<h2>Historik</h2>{timeline_html(r['timeline'])}")

    async def run_page(request: Request) -> Response:
        r = await office.run(int(request.path_params["id"]))
        if r is None:
            return page("Finns inte", "<p>Ingen sådan körning.</p>")
        rows = [("Roll", who(r["role"])), ("Uppgift", e(r["task"]) + f" ({e(r['kind'])})"), ("Tid", e(r["at"])), ("Modell", e(r["model"])),
                ("Längd", f"{(r['duration_ms'] or 0) / 1000:.0f} s"), ("Kostnad", f"{r['cost_usd']:.3f} $" if r["cost_usd"] is not None else "–"),
                ("Tokens", f"in {e(r['input_tokens'])}, ut {e(r['output_tokens'])}, cache {e(r['cache_read_tokens'])}/{e(r['cache_write_tokens'])}, "
                           f"kontext {e(r['context_tokens'])}"),
                ("Steg", e(r["turns"])), ("Kvot", e(r["rate_limit"])), ("Fel", e(r["error"]) if r["is_error"] else "nej")]
        trace = "".join(f"<li>{e(t['tool'])}: {e(t['input'])}{' · fel' if t.get('error') else ''}</li>" for t in r["trace"])
        return page(f"Körning {r['id']}",
                    "<table>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows) + "</table>"
                    f"<h2>Verktyg</h2>{'<ol>' + trace + '</ol>' if trace else '<p>Inga.</p>'}"
                    f"<h2>Utdata (början)</h2><pre>{linked(r['output'] or '')}</pre>")

    async def case_page(request: Request) -> Response:
        ref = request.path_params.get("ref", "")
        q = request.query_params.get("q", "").strip()
        form = '<form action="/arende/"><input name="q" placeholder="#S3, #F210 eller några ord" value="' + e(q) + '"> <button>Sök</button></form>'
        if not ref:
            if q and refs.PATTERN.fullmatch(q):
                m = refs.PATTERN.fullmatch(q)
                return RedirectResponse(f"/arende/{m.group(1).upper()}{m.group(2)}")
            result = f"<pre>{linked(await refs.lookup(db, q, limit=40))}</pre>" if q else ""
            return page("Ärenden", form + result)
        c = await office.case(ref)
        if c is None:
            return page(ref, form + "<p>Inget ärende med det id:t.</p>")
        parts = [form, f"<pre>{linked(c['about'])}</pre>"]
        if c["decisions"]:
            parts.append("<h2>Beslut</h2><ul>" + "".join(f"<li>{e(d['at'])} · {e(d['module'])} · {e(d['tier'])}: {e(d['reason'])}</li>"
                                                        for d in c["decisions"]) + "</ul>")
        if c["messages"]:
            parts.append("<h2>Meddelanden</h2><ul>" + "".join(f"<li>{e(m['at'])} · {who(m['from'])}: {linked(m['text'])}</li>" for m in c["messages"]) + "</ul>")
        if c["posts"]:
            parts.append("<h2>Post</h2><ul>" + "".join(
                f"<li>{e(p['at'])} · {linked(p['ref'])} {who(p['from'])} → {who(p['to'])} ({e(p['kind'])}, {e(p['status'])}): {linked(p['text'])}</li>"
                for p in c["posts"]) + "</ul>")
        if c["bookings"]:
            parts.append("<h2>Bokningar</h2><ul>" + "".join(
                f"<li>{linked(b['ref'])} {e(b['kind'])}, {e(b['status'])}, {e(b['due'])}: {e(b['title'])}</li>" for b in c["bookings"]) + "</ul>")
        return page(c["ref"], "".join(parts))

    async def archive_page(request: Request) -> Response:
        rel = request.path_params.get("path", "").strip("/")
        if not rel:
            return page("Arkivet", "<ul>" + "".join(f'<li><a href="/arkiv/{n}/">{n}/</a></li>' for n in roots) + "</ul>")
        root_name, _, sub = rel.partition("/")
        if root_name not in roots:
            return page("Finns inte", "<p>Ingen sådan mapp.</p>")
        root = roots[root_name].resolve()
        path = (root / sub).resolve()
        if not path.is_relative_to(root) or any(part.startswith(".") for part in path.relative_to(root).parts) or not path.exists():
            return page("Finns inte", "<p>Ingen sådan fil.</p>")
        up = rel.rsplit("/", 1)[0] if "/" in rel else ""
        back = f'<p><a href="/arkiv/{e(up)}{"/" if up else ""}">↑ {e(up or "Arkivet")}</a></p>'
        if path.is_dir():
            entries = sorted((p for p in path.iterdir() if not p.name.startswith(".")), key=lambda p: (p.is_file(), p.name))
            items = "".join(f'<li><a href="/arkiv/{e(rel)}/{e(p.name)}{"/" if p.is_dir() else ""}">{e(p.name)}{"/" if p.is_dir() else ""}</a></li>'
                            for p in entries)
            return page(rel + "/", back + (f"<ul>{items}</ul>" if items else "<p>Tom.</p>"))
        if path.suffix not in TEXT_SUFFIXES:
            return page(rel, back + f"<p>{path.stat().st_size} byte, inte text.</p>")
        return page(rel, back + f"<pre>{linked(path.read_text(errors='replace'))}</pre>")

    async def tables_page(request: Request) -> Response:
        name = request.path_params.get("name")
        tables = [r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        if not name:
            counts = [(t, (await db.fetchone(f'SELECT count(*) AS n FROM "{t}"'))["n"]) for t in tables]
            return page("Tabellerna", "<ul>" + "".join(f'<li><a href="/tabeller/{t}">{t}</a> ({n})</li>' for t, n in counts) + "</ul>")
        if name not in tables:  # only real table names reach the SQL below
            return page("Finns inte", "<p>Ingen sådan tabell.</p>")
        offset = max(0, int(request.query_params.get("sida", "0") or 0)) * PAGE
        rows = await db.fetchall(f'SELECT * FROM "{name}" ORDER BY rowid DESC LIMIT ? OFFSET ?', (PAGE, offset))
        total = (await db.fetchone(f'SELECT count(*) AS n FROM "{name}"'))["n"]
        if not rows:
            return page(name, "<p>Inga rader.</p>")
        cols = rows[0].keys()
        head = "<tr>" + "".join(f"<th>{e(c)}</th>" for c in cols) + "</tr>"
        body = "".join("<tr>" + "".join(f"<td>{linked(str(r[c]) if r[c] is not None else '', 300)}</td>" for c in cols) + "</tr>" for r in rows)
        sida = offset // PAGE
        nav = (f'<p>Rad {offset + 1}–{offset + len(rows)} av {total}, nyast först. '
               + (f'<a href="?sida={sida - 1}">← nyare</a> ' if sida else "")
               + (f'<a href="?sida={sida + 1}">äldre →</a>' if offset + len(rows) < total else "") + "</p>")
        return page(name, nav + f'<table border="1">{head}{body}</table>' + nav)

    # The same data as JSON, for the illustrated office later.
    async def api_now(request: Request) -> Response:
        return JSONResponse(await office.now())

    async def api_timeline(request: Request) -> Response:
        role = request.query_params.get("roll")
        return JSONResponse(await office.timeline(int(request.query_params.get("antal", "60")), role if role in ROLES else None))

    async def api_role(request: Request) -> Response:
        role = request.path_params["role"]
        return JSONResponse(await office.role(role)) if role in ROLES else JSONResponse({"error": "no such role"}, 404)

    async def api_run(request: Request) -> Response:
        r = await office.run(int(request.path_params["id"]))
        return JSONResponse(r) if r else JSONResponse({"error": "no such run"}, 404)

    async def api_case(request: Request) -> Response:
        c = await office.case(request.path_params["ref"])
        return JSONResponse(c) if c else JSONResponse({"error": "no such case"}, 404)

    return Starlette(routes=[
        Route("/", office_page),
        Route("/fragment/nu", now_fragment),
        Route("/fragment/tidslinje", timeline_fragment),
        Route("/roll/{role}", role_page),
        Route("/korning/{id:int}", run_page),
        Route("/arende/", case_page),
        Route("/arende/{ref}", case_page),
        Route("/arkiv/", archive_page),
        Route("/arkiv/{path:path}", archive_page),
        Route("/tabeller/", tables_page),
        Route("/tabeller/{name}", tables_page),
        Route("/api/nu", api_now),
        Route("/api/tidslinje", api_timeline),
        Route("/api/roll/{role}", api_role),
        Route("/api/korning/{id:int}", api_run),
        Route("/api/arende/{ref}", api_case),
    ])


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield  # the engine handles SIGTERM/SIGINT itself and stops the site with it


def start(app: Starlette, host: str, port: int) -> tuple[asyncio.Task, _Server]:
    server = _Server(uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off", access_log=False))
    return asyncio.create_task(server.serve(), name="site"), server
