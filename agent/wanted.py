"""Efterlysningar (instance/granskning/2026-11-02-omgang-10-sokningen.md): what the owner asks Audax to look for, worked
out before it is watched.

On 26 Oct a friend asked to be told of "a Swish illustration competition". The chat searched once and said there was
none, and a news search on her own words ran 97 times without a hit. The competition is called "Årets kort", comes
back every November, and last year's round was written about in three places a single search would have found. So:

- **Investigate before watching.** A request becomes an efterlysning (#J) with status `investigating`. Researcher
  works out in a run of its own what the thing is called, who is behind it, when it has happened before (with dates
  and addresses), whether it recurs and when, and where it is announced; then it chooses the watches.
- **What happened before is a test case.** The code checks each chosen watch against the earlier rounds: a page that
  doesn't mention any of the names, or a news search that wouldn't have found last year's round, fails, and the role
  sees it.
- **No hit is a question, not an answer.** A window that comes near, a watch that stays quiet, and a miss (the owner
  found one themselves) all start a new investigation instead of the same search again.

The code owns the life cycle, the checks and when it wakes; the role investigates and chooses. The structured parts
are typed fields (JSON lists in the item's data); only the reasons are prose.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from agent import direction_ops as ops
from agent import routines, trail
from agent.claude_gate import ClaudeGate, ClaudeTask, Priority, QuotaPaused
from agent.config import Settings
from agent.db import Database
from agent.forms import FormError
from agent.items import Items, ref

log = logging.getLogger(__name__)
starter: Callable[[], None] | None = None   # set by main: investigate soon, when the chat made one

AGAIN_AFTER = timedelta(hours=3)        # an investigation that didn't finish is tried again after this
MAX_TRIES = 3                           # investigations in a row whose watches all fail the check, before it watches anyway
LOOK_AGAIN = timedelta(days=14)         # without a known window: when the request is looked at again
BEFORE_WINDOW = timedelta(days=7)       # a known window: investigate this long before it opens
IN_WINDOW_EVERY = timedelta(days=3)     # inside the window: the organiser's own page is read this often
WINDOW_PACE = 120                       # minutes between checks of the watches inside the window
QUIET_POLLS = 60                        # a watch checked this often without bringing anything is quiet
QUIET_AFTER = timedelta(days=7)         # and only once the last investigation is this old

INVESTIGATION = {
    "type": "object",
    "properties": {
        "interpretation": {"type": "string", "description": "Swedish: what the owner is looking for, as you now understand it"},
        "names": {"type": "array", "items": {"type": "string"},
                  "description": "What the thing is actually called, and what else it is called (the owner's words are often not the world's)"},
        "organizer": {"type": ["string", "null"]},
        "past": {"type": "array", "description": "Earlier rounds or occurrences you found, each with its date and address",
                 "items": {"type": "object", "properties": {
                     "date": {"type": "string", "description": "YYYY-MM-DD: when it was announced or opened"},
                     "title": {"type": "string"}, "url": {"type": "string"}},
                     "required": ["date", "title", "url"]}},
        "cycle": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["yearly", "recurring", "once", "unknown"]},
            "window_start": {"type": ["string", "null"], "description": "MM-DD: when a round usually opens (yearly)"},
            "window_end": {"type": ["string", "null"], "description": "MM-DD: when it usually closes (yearly)"},
            "note": {"type": "string", "description": "Swedish: what the dates rest on"}},
            "required": ["kind", "window_start", "window_end", "note"]},
        "channels": {"type": "array", "description": "Where it is announced: the organiser's own page, feeds, news, aggregators, social media",
                     "items": {"type": "object", "properties": {
                         "kind": {"type": "string", "enum": ["organizer", "feed", "news", "aggregator", "social", "other"]},
                         "name": {"type": "string"}, "url": {"type": ["string", "null"]},
                         "why": {"type": "string", "description": "Swedish, one line"}},
                         "required": ["kind", "name", "url", "why"]}},
        "watches": {"type": "array", "description": "What to watch from now on: the channels that will show the next one first. "
                    "Each is checked against the earlier rounds; prefer the organiser's own page and the words the world uses.",
                    "items": {"type": "object", "properties": {
                        "type": {"type": "string", "enum": ["page", "feed", "topic"]},
                        "name": {"type": "string"},
                        "target": {"type": "string", "description": "page/feed: its URL; topic: the news search query"},
                        "lang": {"type": "string", "enum": ["sv", "en"]},
                        "why": {"type": "string", "description": "Swedish, one line"}},
                        "required": ["type", "name", "target", "lang", "why"]}},
        "current": {"type": "object", "description": "Whether one is open or announced right now, read on its own page",
                    "properties": {
                        "found": {"type": "boolean"},
                        "title": {"type": ["string", "null"]}, "url": {"type": ["string", "null"]},
                        "summary": {"type": ["string", "null"], "description": "Swedish: the facts, from the page"},
                        "why_you": {"type": ["string", "null"], "description": "Swedish: why it is what the owner asked for"},
                        "kind": {"type": ["string", "null"], "enum": ["opportunity", "event", "news", None]},
                        "deadline": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                        "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                        "organization": {"type": ["string", "null"]},
                        "checked": {"type": ["string", "null"], "enum": ["page", "other_sites", "search_results", None]}},
                    "required": ["found"]},
        "question": {"type": ["object", "null"], "description": "Only when you can't tell what the owner means: one question, with your guess",
                     "properties": {
                         "text": {"type": "string", "description": "Swedish: what is unclear"},
                         "guess": {"type": "string", "description": "Swedish: your best guess, and how sure"},
                         "examples": {"type": "array", "items": {"type": "string"},
                                      "description": "Swedish: two or three concrete candidates the owner can point at ('menar du den här?')"}},
                     "required": ["text", "guess", "examples"]},
        "post_mortem": {"type": ["string", "null"], "description": "A miss only. Swedish: where it was announced, which of the "
                        "watches could have seen it and why none did, and what you changed so the next one is caught"},
        "summary": {"type": "string", "description": "Swedish: what you found out and did, briefly"},
    },
    "required": ["interpretation", "names", "past", "cycle", "channels", "watches", "current", "summary"],
}


def _json(item: dict, field: str, default):
    try:
        return json.loads(item.get(field) or "null") or default
    except (TypeError, ValueError):
        return default


def _today(now: datetime) -> date:
    return now.date()


def window(cycle: dict, today: date) -> tuple[date, date] | None:
    """The next (or current) window of a yearly thing, from its MM-DD bounds."""
    if (cycle or {}).get("kind") != "yearly" or not cycle.get("window_start"):
        return None
    try:
        sm, sd = (int(x) for x in cycle["window_start"].split("-"))
        em, ed = (int(x) for x in (cycle.get("window_end") or cycle["window_start"]).split("-"))
        for year in (today.year - 1, today.year, today.year + 1):
            start = date(year, sm, sd)
            end = date(year + (1 if (em, ed) < (sm, sd) else 0), em, ed)
            if end >= today:
                return start, end
    except ValueError:
        return None
    return None


def next_look(cycle: dict, today: date) -> tuple[date, bool]:
    """When the request is looked at again, and whether today is inside (or just before) its window."""
    w = window(cycle, today)
    if w is None:
        return today + LOOK_AGAIN, False
    start, end = w
    if today >= start - BEFORE_WINDOW:
        return min(today + IN_WINDOW_EVERY, end + timedelta(days=1)), True
    return start - BEFORE_WINDOW, False


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", (text or "").casefold()))


def mentions(text: str, names: list[str]) -> str | None:
    """The name whose words all appear in the text, whatever the hyphens, case and asides: a headline says "årets
    Swish-vykort" for "Årets vykort (Swish)" (2 Nov: the literal match failed every check)."""
    have = _words(text)
    for n in names:
        for variant in (re.sub(r"\([^)]*\)", " ", n), *re.findall(r"\(([^)]*)\)", n)):   # "Getswish AB (Swish)": either
            need = {w for w in _words(variant) if len(w) > 2}
            if need and need <= have:
                return n
    return None


async def backtest(watch: dict, names: list[str], past: list[dict], client=None) -> dict:
    """Would this watch have seen it before? pass, fail or unverifiable, with what it rests on."""
    from agent.netguard import public_client
    from agent.sources.pages import page_text
    from agent.sources.streams import USER_AGENT, parse_feed

    result = {"watch": watch["id"], "type": watch["type"]}
    if not [n for n in names if n.strip()]:
        return {**result, "result": "unverifiable", "note": "inga namn att pröva mot"}
    own = client is None
    client = client or public_client(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    try:
        if watch["type"] == "page":
            r = await client.get(watch["target"])
            if r.status_code != 200:
                return {**result, "result": "fail", "note": f"sidan svarade {r.status_code}"}
            text = " ".join(page_text(r.text, str(r.url)))
            if not text.strip():
                return {**result, "result": "fail", "note": "sidan har ingen läsbar text (byggs troligen med JavaScript): en sidbevakning ser inget där"}
            hit = mentions(text, names)
            return {**result, "result": "pass" if hit else "fail",
                    "note": f"sidan nämner {hit!r}" if hit else "sidan nämner inget av namnen"}
        if watch["type"] == "feed":
            r = await client.get(watch["target"])
            if r.status_code != 200:
                return {**result, "result": "fail", "note": f"flödet svarade {r.status_code}"}
            hit = next((i.title for i in parse_feed(r.content) if mentions(f"{i.title} {i.text}", names)), None)
            return {**result, "result": "pass" if hit else "unverifiable",
                    "note": f"flödet har {hit[:70]!r}" if hit else "flödet visar bara det senaste; inget av namnen där just nu"}
        dated = [p for p in past if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(p.get("date", "")))]
        if not dated:
            return {**result, "result": "unverifiable", "note": "ingen tidigare omgång med datum att pröva mot"}
        hl, gl, ceid = ("sv", "SE", "SE:sv") if watch.get("lang") == "sv" else ("en-US", "US", "US:en")
        for p in dated[:3]:
            d = date.fromisoformat(p["date"])
            q = f"{watch['target']} after:{d - timedelta(days=45)} before:{d + timedelta(days=45)}"
            r = await client.get(f"https://news.google.com/rss/search?q={quote(q)}&hl={hl}&gl={gl}&ceid={ceid}")
            if r.status_code == 200:
                items = parse_feed(r.content)
                if hit := next((i.title for i in items if mentions(f"{i.title} {i.text}", names)), None):
                    return {**result, "result": "pass", "note": f"hade hittat omgången {p['date']}: {hit[:70]!r}"}
        return {**result, "result": "fail", "note": "nyhetssökningen hade inte hittat någon av de tidigare omgångarna"}
    except Exception as e:
        return {**result, "result": "unverifiable", "note": f"gick inte att pröva just nu ({type(e).__name__})"}
    finally:
        if own:
            await client.aclose()


async def create(db: Database, asked: str, by: str, *, kind: str = "request", interpretation: str = "",
                 missed_url: str = "") -> int:
    """A new efterlysning, to be investigated at once."""
    title = (interpretation or asked).strip()[:90]
    return await Items(db).create(
        "wanted", title, by, kind=kind, owner="researcher", source={"type": "owner"},
        data={"asked": asked, "interpretation": interpretation or None, "missed_url": missed_url or None,
              "why_now": "en miss: ägaren hittade det själv" if kind == "miss" else "ny efterlysning"},
        note="ägarens ord")


class Wanted:
    def __init__(self, settings: Settings, db: Database, gate: ClaudeGate, memory):
        self.settings = settings
        self.db = db
        self.gate = gate
        self.memory = memory
        self.web = None
        self.on_found: Callable[[], Awaitable[None]] | None = None   # triage and the dispatcher, for a find
        self.items = Items(db)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    # --- when -----------------------------------------------------------------------------

    async def next_up(self, now: datetime) -> tuple[dict, str] | None:
        """The efterlysning to investigate now, and why."""
        today = _today(now)
        for w in await self.items.find("wanted", status=("investigating", "waiting", "watching", "found"), order="id"):
            last = w.get("investigated_at")
            last_at = datetime.fromisoformat(last).replace(tzinfo=self.tz) if last else None
            if w["status"] == "investigating":
                if last_at is None or now - last_at >= AGAIN_AFTER:
                    return w, w.get("why_now") or "ny efterlysning"
            elif w["status"] == "waiting":
                if q := w.get("question"):
                    asked = await self.items.get(int(q))
                    if asked is None or asked["status"] in ("answered", "unanswered", "settled", "dropped"):
                        return w, "frågan till ägaren är besvarad" if asked and asked["status"] == "answered" else "frågan fick inget svar"
            elif w["review_at"] and w["review_at"][:10] <= f"{today}":
                inside = next_look(_json(w, "cycle", {}), today)[1]
                return w, "fönstret är nära eller öppet: läs arrangörens egen sida" if inside else "dags att se över"
            elif w["status"] == "watching" and last_at and now - last_at >= QUIET_AFTER and await self._quiet(w, last):
                return w, f"bevakningarna har inte gett något på {QUIET_POLLS} kontroller"
        return None

    async def _quiet(self, w: dict, since: str) -> bool:
        watches = _json(w, "watches", [])
        if not watches:
            return True
        stats = {r["stream"]: dict(r) for r in await self.db.fetchall(
            f"SELECT stream, polls FROM stream_stats WHERE stream IN ({','.join('?' * len(watches))})", tuple(watches))}
        if sum((stats.get(s) or {}).get("polls", 0) or 0 for s in watches) < QUIET_POLLS:
            return False
        since_utc = datetime.fromisoformat(since).replace(tzinfo=self.tz).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
        row = await self.db.fetchone(
            f"SELECT count(*) AS n FROM events WHERE created_at >= ? AND source IN ({','.join('?' * len(watches))})",
            (since_utc, *[f"stream:{s}" for s in watches]))
        return row["n"] == 0

    async def tick(self, now: datetime | None = None) -> bool:
        """Investigates one efterlysning that is due. True if one ran."""
        now = now or datetime.now(self.tz)
        if (up := await self.next_up(now)) is None:
            return False
        w, why = up
        return await self.investigate(w, why, now)

    # --- the investigation ----------------------------------------------------------------

    async def investigate(self, w: dict, why: str, now: datetime) -> bool:
        from agent.prompts import prompt as prompt_template
        from agent.pursuits import judged_against
        from agent.tools import TurnContext, build_web_tools

        await self.items.update(w["id"], "system", data={"investigated_at": f"{now:%Y-%m-%dT%H:%M}", "why_now": why},
                                status="investigating" if w["status"] == "waiting" else None)
        turn = TurnContext()
        web_servers, web_names = build_web_tools(self.web, turn) if self.web else ({}, [])
        answer = ""
        if q := w.get("question"):
            asked = await self.items.get(int(q))
            if asked and asked.get("answer"):
                answer = f"The owner answered your question: {asked['answer']}"
        prompt = prompt_template("investigate").format(
            ref=ref(w), kind=w["kind"], asked=w.get("asked") or w["title"], why=why, today=f"{now:%Y-%m-%d}",
            known=self._known(w) + (f"\n{answer}" if answer else ""),
            missed=f"The owner found this one themselves: {w['missed_url']}" if w.get("missed_url") else "",
            memory=await judged_against(self.db, self.memory, self.settings.timezone),
        )
        try:
            result = await self.gate.run(ClaudeTask(
                kind="investigate", prompt=prompt, priority=Priority.RESEARCH, model=self.settings.models.research,
                allowed_tools=["WebSearch", *web_names], mcp_servers=web_servers, output_schema=INVESTIGATION, max_turns=40))
        except QuotaPaused:
            log.info("investigation %s postponed: quota paused", ref(w))
            return False
        out = result.structured
        if result.is_error or not isinstance(out, dict):
            log.warning("investigation %s failed: %s", ref(w), result.error or "no structured answer")
            return False
        await self.apply(w, out, now)
        if out.get("current", {}).get("found") and self.on_found is not None:
            await self.on_found()
        return True

    def _known(self, w: dict) -> str:
        parts = []
        for field in ("interpretation", "names", "organizer", "past", "cycle", "channels", "checks", "finding", "post_mortem"):
            if w.get(field):
                parts.append(f"{field}: {w[field]}")
        return "\n".join(parts) or "(nothing yet: this is the first investigation)"

    async def apply(self, w: dict, out: dict, now: datetime) -> list[dict]:
        """Writes the investigation, sets up the watches, checks them against the earlier rounds and decides what next."""
        from agent import opportunities
        from agent.research import normalize_url

        today = _today(now)
        names = [n for n in out.get("names") or [] if n.strip()]
        past = out.get("past") or []
        cycle = out.get("cycle") or {}
        # The watches: made or kept, the ones no longer chosen paused.
        before = set(_json(w, "watches", []))
        chosen = []
        for x in out.get("watches") or []:
            sid = ("topic-" if x["type"] == "topic" else "") + ops.slug(f"j{w['id']}-{x['name']}")   # as ops names them
            if x["type"] != "topic":   # an address already watched by another source is that source
                sid = next((r["_config"]["stream"]["id"] for r in await routines.all_routines(self.db, "stream")
                            if r["_config"]["stream"].get("url") == x["target"]), sid)
            note = f"Efterlysning {ref(w)}: {out.get('interpretation') or w['title']}. Namn: {', '.join(names)}."
            existing = await routines.find(self.db, "stream", sid)
            if x["type"] == "topic":
                if existing is None:
                    await ops.add_topic(self.db, f"j{w['id']}-{x['name']}", x["target"], x["lang"], note, urgent=True)
                else:
                    await routines.put_stream(self.db, {**existing["_config"]["stream"], "params": {"query": x["target"], "lang": x["lang"]},
                                                        "note": note, "enabled": True}, "researcher", note="efterlysningen utreddes igen")
            elif existing is None:
                await ops.add_page(self.db, sid, x["target"], note, "event", "rss" if x["type"] == "feed" else "page", urgent=True)
            elif sid.startswith(f"j{w['id']}-"):   # its own: follows the investigation; another source's is only read
                await routines.put_stream(self.db, {**existing["_config"]["stream"], "url": x["target"], "note": note, "enabled": True},
                                          "researcher", note="efterlysningen utreddes igen")
            if await routines.find(self.db, "stream", sid):
                chosen.append({**x, "id": sid})
        for sid in before - {c["id"] for c in chosen}:   # its own watches no longer chosen, and a request's old news search
            await ops.disable_sense(self.db, sid, f"inte längre vald för {ref(w)}")
        known_as = names + ([out["organizer"]] if out.get("organizer") else [])   # a headline may name only the organiser
        checks = [await backtest(c, known_as, past) for c in chosen]
        start_of = next_look(cycle, today)
        if start_of[1]:   # inside the window: its watches are checked often
            for c in chosen:
                await ops.tune_sense(self.db, c["id"], WINDOW_PACE, f"fönstret för {ref(w)}")
        # What is open now goes to triage as something the owner asked for.
        found = out.get("current") or {}
        finding_note = None
        if found.get("found") and found.get("url") and found.get("title"):
            f = {"title": found["title"], "url": found["url"], "summary": found.get("summary") or "",
                 "why_you": found.get("why_you") or f"Det ägaren bad om: {w.get('asked')}", "kind": found.get("kind") or "opportunity",
                 "deadline": found.get("deadline"), "date": found.get("date"), "organization": found.get("organization"),
                 "checked": found.get("checked") or "search_results", "unconfirmed": None, "identity": "new",
                 "query": f"efterlysning {ref(w)}", "asked_for": True}
            oid, _ = await opportunities.record(self.db, f)
            if oid is not None:
                f["opportunity"] = oid
            await self.db.insert("events", ignore=True, source=f"wanted:{w['id']}", kind=f["kind"],
                                 dedupe_key=normalize_url(f["url"]), payload=json.dumps(f, ensure_ascii=False))
            await trail.emit("print", "director", n=1)
            finding_note = f"{found['title']} ({found['url']})"
        # A question to the owner, when what they mean is unclear.
        status, question_id = "watching", None
        if q := out.get("question"):
            try:
                question_id = await self.items.create(
                    "question", q["text"][:120], "researcher", kind="clarify", owner="researcher",
                    data={"to": "owner", "why": f"Efterlysningen {ref(w)} ({w.get('asked')}) går inte att leta efter förrän det är klart.",
                          "guess": q.get("guess"), "context": "Exempel att visa: " + "; ".join(q.get("examples") or [])},
                    review_at=f"{today + timedelta(days=7)}", note=f"för {ref(w)}")
                await self.items.link(question_id, w["id"], "asks", "researcher")
                status = "waiting"
            except FormError as e:
                log.warning("investigation %s: the question wasn't made: %s", ref(w), e)
        tries = int(w.get("tries") or 0) + 1
        passed = [c for c in checks if c["result"] == "pass"]
        if status != "waiting" and past and chosen and not passed and tries < MAX_TRIES:
            status = "investigating"   # nothing chosen would have seen it before: look for better ways, not the same again
        if status == "watching" and found.get("found") and cycle.get("kind") == "once":
            status = "found"
        review = start_of[0] if status in ("watching", "found") else (today + timedelta(days=7) if status == "waiting" else None)
        data = {
            "interpretation": out.get("interpretation"), "names": json.dumps(names, ensure_ascii=False),
            "organizer": out.get("organizer"), "past": json.dumps(past, ensure_ascii=False),
            "cycle": json.dumps(cycle, ensure_ascii=False), "channels": json.dumps(out.get("channels") or [], ensure_ascii=False),
            "watches": json.dumps([c["id"] for c in chosen]), "checks": json.dumps(checks, ensure_ascii=False),
            "tries": 0 if passed or status != "investigating" else tries,
            "question": str(question_id) if question_id else None,
            "post_mortem": out.get("post_mortem"),
            **({"finding": finding_note} if finding_note else {}),
        }
        failed = [c for c in checks if c["result"] == "fail"]
        note = out.get("summary") or "utredd"
        if failed:
            note += " · underkända: " + ", ".join(f"{c['watch']} ({c['note']})" for c in failed)
        await self.items.update(w["id"], "researcher", status=status, review_at=f"{review}" if review else None,
                                title=(out.get("interpretation") or w["title"])[:90], data=data, note=note[:1000])
        log.info("investigation %s: %s, %d watches (%d pass, %d fail), %s", ref(w), status, len(chosen), len(passed), len(failed),
                 "found one" if finding_note else "nothing open")
        return checks


async def for_review(db: Database) -> str:
    """Researcher's efterlysningar in its planning run: what each is, its watches and how they fared against the past."""
    rows = await Items(db).find("wanted", status=("investigating", "waiting", "watching", "found"))
    if not rows:
        return ""
    lines = []
    for w in rows:
        checks = _json(w, "checks", [])
        mark = ", ".join(f"{c['watch']} {c['result']}" for c in checks) or "inga bevakningar ännu"
        lines.append(f"- {Items.line(w)} · namn {', '.join(_json(w, 'names', [])) or '?'} · cykel "
                     f"{_json(w, 'cycle', {}).get('kind', '?')} · nästa {w['review_at'] or '-'} · {mark}")
    return ("Efterlysningar (what owners asked to have found; each is investigated in a run of its own, and its watches are "
            "checked against the earlier rounds; one that keeps failing is worth a new way of looking):\n" + "\n".join(lines))


async def overview(db: Database, tz: str) -> str:
    """For the chat, when the owner asks what Audax is looking for: Swedish facts, not to be read out as they are."""
    rows = await Items(db).find("wanted", status=("investigating", "waiting", "watching", "found"))
    if not rows:
        return "Inga efterlysningar."
    today = datetime.now(ZoneInfo(tz)).date()
    words = {"investigating": "utreds", "waiting": "väntar på svar från ägaren", "watching": "bevakas", "found": "hittad"}
    out = []
    for w in rows:
        cycle = _json(w, "cycle", {})
        win = window(cycle, today)
        out.append(f"- {w['title']}: {words[w['status']]}"
                   + (f"; heter {', '.join(_json(w, 'names', []))}" if w.get("names") else "")
                   + (f"; brukar komma {win[0]:%d %b}–{win[1]:%d %b}" if win else "")
                   + (f"; senast hittad: {w['finding']}" if w.get("finding") else "")
                   + (f"; ses över {w['review_at']}" if w["review_at"] else ""))
    return "\n".join(out)


async def migrate(db: Database) -> list[str]:
    """Once: every news watch an owner asked for becomes an efterlysning to investigate, the same in every instance."""
    if await db.kv_get("wanted.migrated"):
        return []
    made = []
    for r in await routines.all_routines(db, "stream"):
        s = r["_config"]["stream"]
        if s.get("type") != "topic" or not s.get("urgent") or r["status"] != "active":
            continue
        query = (s.get("params") or {}).get("query") or s["id"]
        wid = await create(db, s.get("note") or query, "system", interpretation=query)
        await Items(db).update(wid, "system", data={"watches": json.dumps([s["id"]])}, note=f"från bevakningen {s['id']}")
        made.append(f"#J{wid} från {s['id']}")
    await db.kv_set("wanted.migrated", True)
    return made
