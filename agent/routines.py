"""Watched sources and search missions as routines (instance/granskning/2026-10-20-omgang-3-flodet.md): items in the
office's core (#N, agent/items.py) instead of config files, so each has an owner, a purpose (what it serves), a
history, and a health.

A routine's `config` holds what the code needs to run it: {"kind": "stream", "stream": {...}} for a watched source
(the same fields sources.yaml had) or {"kind": "mission", "mission": {...}} for a search mission (research.yaml's).
The readers in the engine are synchronous, so the active routines are kept in memory and refreshed whenever one
changes. At the first start after the move the old files are read in once and kept as *.imported.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import yaml

from agent.config import instance_config
from agent.db import Database
from agent.forms import FormError
from agent.items import Items

log = logging.getLogger(__name__)

_streams: list[dict] = []    # the active streams' configs (sources.yaml format), in id order
_missions: list[dict] = []   # every mission's config, enabled or not (research.yaml format)
_loaded = False
_cadences: dict[str, dict] = {}   # skill -> its routine's config, for the rhythms the roles set themselves
on_streams_changed: list[Callable[[], Awaitable[None]]] = []   # e.g. the ingest restarting its pollers


def streams() -> list[dict] | None:
    """The active streams, or None before the routines are loaded (then the old file is read)."""
    return list(_streams) if _loaded else None


def missions() -> list[dict] | None:
    return list(_missions) if _loaded else None


def _config(item: dict) -> dict:
    try:
        return json.loads(item.get("config") or "{}")
    except ValueError:
        return {}


async def all_routines(db: Database, kind: str) -> list[dict]:
    """Every routine of a kind (stream or mission), with its config, whatever its status."""
    out = []
    for r in await Items(db).find("routine", order="id", limit=1000):
        c = _config(r)
        if c.get("kind") == kind:
            out.append({**r, "_config": c})
    return out


async def find(db: Database, kind: str, key: str) -> dict | None:
    for r in await all_routines(db, kind):
        if r["_config"].get(kind, {}).get("id") == key:
            return r
    return None


async def refresh(db: Database) -> None:
    global _streams, _missions, _loaded
    before = json.dumps(_streams, sort_keys=True)
    _streams = [r["_config"]["stream"] for r in await all_routines(db, "stream") if r["status"] == "active"]
    _missions = [{**r["_config"]["mission"], "enabled": r["status"] == "active"} for r in await all_routines(db, "mission")]
    _loaded = True
    global _cadences
    _cadences = {}
    for r in await Items(db).find("routine", status="active", limit=1000):
        c = _config(r)
        if c.get("skill"):
            _cadences[c["skill"]] = c
    if json.dumps(_streams, sort_keys=True) != before:
        for cb in on_streams_changed:   # in the background: a poller may be what changed its own routine
            asyncio.get_running_loop().create_task(_safely(cb))


async def _safely(cb) -> None:
    try:
        await cb()
    except Exception:
        log.exception("restarting streams after a routine changed")


def cadence(skill: str, key: str, default: float) -> float:
    """How often a skill runs, from a routine the roles can change ({"skill": "triage", "every_minutes": 60}); the
    default until one exists."""
    try:
        return float(_cadences.get(skill, {}).get(key, default))
    except (TypeError, ValueError):
        return default


async def watch(db: Database, item: dict) -> None:
    """Items watcher: a routine changed, so what runs changes too."""
    if item["form"] == "routine":
        await refresh(db)


# --- writing ------------------------------------------------------------------------------

async def put_stream(db: Database, stream: dict, by: str, *, note: str = "", parent: int | None = None, title: str | None = None) -> tuple[int, bool]:
    """Adds a watched source, or changes the one with the same id. Returns (item id, created)."""
    items = Items(db)
    existing = await find(db, "stream", stream["id"])
    what = title or stream.get("note") or stream["id"]
    if existing is None:
        rid = await items.create("routine", _title(stream, what), by, owner="researcher", parent=parent,
                                 data={"what": f"Bevakar {stream.get('url') or stream['id']} ({stream['type']})",
                                       "cadence": f"var {stream.get('poll_minutes', 30):g}:e minut",
                                       "config": json.dumps({"kind": "stream", "stream": stream}, ensure_ascii=False)},
                                 note=note or "ny bevakning")
        return rid, True
    merged = {**existing["_config"]["stream"], **stream}
    await items.update(existing["id"], by, status="active" if merged.get("enabled", True) else "paused",
                       data={"config": json.dumps({"kind": "stream", "stream": merged}, ensure_ascii=False),
                             "cadence": f"var {merged.get('poll_minutes', 30):g}:e minut"}, note=note or "ändrad")
    return existing["id"], False


async def set_stream(db: Database, stream_id: str, by: str, note: str = "", **fields) -> bool:
    existing = await find(db, "stream", stream_id)
    if existing is None:
        return False
    await put_stream(db, {**existing["_config"]["stream"], **fields}, by, note=note)
    return True


async def pause(db: Database, kind: str, key: str, by: str, why: str, health: str | None = None) -> bool:
    existing = await find(db, kind, key)
    if existing is None:
        return False
    data = {"health": health} if health else None
    await Items(db).update(existing["id"], by, status="paused", data=data, note=why)
    return True


async def put_mission(db: Database, mission: dict, by: str, *, note: str = "", parent: int | None = None) -> tuple[int, bool]:
    items = Items(db)
    existing = await find(db, "mission", mission["id"])
    mission = dict(mission)
    enabled = mission.pop("enabled", True)
    if existing is None:
        rid = await items.create("routine", f"Sökuppdrag: {mission['id']}", by, owner="researcher", parent=parent,
                                 status="active" if enabled else "paused",
                                 data={"what": mission.get("goal", "")[:300], "cadence": f"var {mission.get('interval_days', 7):g}:e dag",
                                       "config": json.dumps({"kind": "mission", "mission": mission}, ensure_ascii=False)},
                                 note=note or "nytt sökuppdrag")
        return rid, True
    merged = {**existing["_config"]["mission"], **mission}
    await items.update(existing["id"], by, status="active" if enabled else "paused",
                       data={"what": merged.get("goal", "")[:300], "cadence": f"var {merged.get('interval_days', 7):g}:e dag",
                             "config": json.dumps({"kind": "mission", "mission": merged}, ensure_ascii=False)}, note=note or "ändrat")
    return existing["id"], False


def _title(stream: dict, what: str) -> str:
    kind = {"rss": "Flöde", "page": "Sida", "topic": "Ämne", "ats": "Jobbsidor", "jobtech": "Platsbanken", "devpost": "Devpost",
            "hn_hiring": "HN hiring", "websocket": "Ström"}.get(stream.get("type", ""), "Källa")
    return f"{kind}: {stream['id']}" if what == stream["id"] else f"{kind}: {stream['id']} ({what[:60]})"


# --- the move from the files ---------------------------------------------------------------

async def import_files(db: Database) -> list[str]:
    """Once: sources.yaml's streams and research.yaml's missions become routines; the files are kept as *.imported."""
    done = []
    sources, research = instance_config("sources.yaml"), instance_config("research.yaml")
    if sources.exists() and not await all_routines(db, "stream"):
        data = yaml.safe_load(sources.read_text()) or {}
        for s in data.get("streams") or []:
            try:
                rid, _ = await put_stream(db, dict(s), "system", note="flyttad från sources.yaml (etapp 5)")
                if not s.get("enabled", True):
                    await Items(db).update(rid, "system", status="paused", note="var avstängd i sources.yaml")
                done.append(s["id"])
            except FormError as e:
                log.warning("couldn't move stream %s: %s", s.get("id"), e)
        rest = {k: v for k, v in data.items() if k != "streams"}
        sources.rename(sources.with_name("sources.yaml.imported"))
        if rest:   # anything else in the file stays where the code reads it
            sources.write_text(yaml.safe_dump(rest, allow_unicode=True, sort_keys=False))
    if research.exists() and not await all_routines(db, "mission"):
        for m in yaml.safe_load(research.read_text()) or []:
            try:
                await put_mission(db, dict(m), "system", note="flyttat från research.yaml (etapp 5)")
                done.append(m["id"])
            except FormError as e:
                log.warning("couldn't move mission %s: %s", m.get("id"), e)
        research.rename(research.with_name("research.yaml.imported"))
    if done:
        log.info("routines: moved %d watches and missions from the files", len(done))
    return done
