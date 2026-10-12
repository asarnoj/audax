"""In-process MCP tools that Claude can call. Exposed as mcp__agent__<name>."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server as _create_sdk_mcp_server
from claude_agent_sdk import tool

from agent.db import Database
from agent.prompts import personalize

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent.builder import Builder
    from agent.controls import Controls
    from agent.inbox import Inbox
    from agent.schedule import Schedule

SERVER_NAME = "agent"


def _personalize_value(v):
    if isinstance(v, str):
        return personalize(v)
    if isinstance(v, dict):
        return {k: _personalize_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_personalize_value(x) for x in v]
    return v


def create_sdk_mcp_server(name: str, version: str = "1.0.0", tools=None):
    """Fills in the owner's name in tool descriptions and schemas ({owner}, {owner_s})."""
    for t in tools or []:
        t.description = personalize(t.description)
        if isinstance(t.input_schema, dict):
            t.input_schema = _personalize_value(t.input_schema)
    return _create_sdk_mcp_server(name, version=version, tools=tools)


@dataclass
class TurnContext:
    """What the current chat turn is about, so tools can link feedback to the right message."""

    replied_to_message_id: int | None = None  # messages.id the user replied to, if any


def tool_name(name: str) -> str:
    return f"mcp__{SERVER_NAME}__{name}"


def _text(t: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": t}]}


def build_chat_tools(
    db: Database,
    turn: TurnContext,
    schedule: Schedule | None = None,
    controls: Controls | None = None,
    builder: Builder | None = None,
    inbox: Inbox | None = None,
    wake_director=None,
) -> tuple[dict[str, Any], list[str]]:
    """Returns (mcp_servers entry, allowed tool names)."""

    @tool(
        "record_feedback",
        "Spara {owner_s} feedback på något du har skickat eller på hur du beter dig "
        "(t.ex. 'mer sånt', 'det där var onödigt', 'skriv kortare', 'varför sa du inte det här?'). "
        "Anropa när {owner} uttrycker en åsikt om innehåll, timing, mängd eller ton.",
        {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["replied", "last_sent", "general"],
                    "description": "replied = meddelandet {owner} svarade på, last_sent = ditt senaste meddelande, general = beteendet i stort",
                },
                "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral", "missed"]},
                "aspect": {"type": "string", "enum": ["content", "timing", "amount", "tone", "other"]},
                "note": {"type": "string", "description": "Kort sammanfattning av feedbacken med {owner_s} egna ord om möjligt"},
            },
            "required": ["target", "sentiment", "aspect", "note"],
        },
    )
    async def record_feedback(args: dict[str, Any]) -> dict[str, Any]:
        message_id = None
        if args["target"] == "replied":
            message_id = turn.replied_to_message_id
        elif args["target"] == "last_sent":
            row = await db.fetchone("SELECT id FROM messages WHERE direction = 'out' ORDER BY id DESC LIMIT 1")
            message_id = row["id"] if row else None

        candidate_id = None
        if message_id is not None:
            row = await db.fetchone("SELECT candidate_id FROM messages WHERE id = ?", (message_id,))
            candidate_id = row["candidate_id"] if row else None

        await db.execute(
            "INSERT INTO feedback (message_id, candidate_id, signal, value, note) VALUES (?, ?, 'text', ?, ?)",
            (message_id, candidate_id, f"{args['sentiment']}:{args['aspect']}", args["note"]),
        )
        return _text("Feedback sparad.")

    tools = [record_feedback]

    if schedule is not None:

        @tool(
            "get_schedule",
            "Hämta {owner_s} schema (TimeEdit + egen kalender) för kommande dagar. "
            "[?] = okänt om passet gäller {owner}, [ibland] = gäller ibland. Varje pass har ett serie-id.",
            {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 21, "description": "Antal dagar från idag"},
                    "include_not_mine": {"type": "boolean", "description": "Ta med pass som {owner} sagt inte gäller hen"},
                },
                "required": ["days"],
            },
        )
        async def get_schedule(args: dict[str, Any]) -> dict[str, Any]:
            return _text(await schedule.format_days(args["days"], args.get("include_not_mine", False)))

        @tool(
            "set_series_relevance",
            "Spara om en serie pass (t.ex. alla övningar i en kurs) gäller {owner}. "
            "Använd när {owner} säger att hen går/inte går på något. Serie-id får du från get_schedule.",
            {
                "type": "object",
                "properties": {
                    "series_id": {"type": "integer"},
                    "relevance": {"type": "string", "enum": ["mine", "not_mine", "sometimes", "unknown"]},
                    "note": {"type": "string", "description": "Valfri kommentar, t.ex. 'bara udda veckor'"},
                },
                "required": ["series_id", "relevance"],
            },
        )
        async def set_series_relevance(args: dict[str, Any]) -> dict[str, Any]:
            ok = await schedule.set_relevance(args["series_id"], args["relevance"], args.get("note"))
            return _text("Sparat." if ok else f"Hittade ingen serie med id {args['series_id']}.")

        tools += [get_schedule, set_series_relevance]

    if controls is not None:
        tools += _control_tools(controls, turn)

    if builder is not None:

        @tool(
            "request_change",
            "Bygg om agenten själv när {owner} vill ändra hur den fungerar: nya källor, ändrat beteende, nya funktioner, "
            "andra inställningar. Ändringen byggs och testas på en separat gren, och {owner} godkänner med en knapp innan "
            "något driftsätts. Beskriv önskan tydligt och konkret, med {owner_s} egna ord och allt du vet (länkar, exempel). "
            "Använd inte det här för preferenser som hör hemma i minnet.",
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Kort, t.ex. 'Lägg till universitetets eventkalender som källa'"},
                    "description": {"type": "string"},
                },
                "required": ["title", "description"],
            },
        )
        async def request_change(args: dict[str, Any]) -> dict[str, Any]:
            try:
                cid = await builder.request(args["title"], args["description"])
            except RuntimeError as e:
                return _text(f"Kan inte starta nu: {e}. Säg det till " + personalize("{owner}") + ".")
            return _text(f"Ändring {cid} byggs nu. Det tar några minuter. " + personalize("{owner} får ett meddelande med en sammanfattning och knappar när den är klar."))

        tools.append(request_change)

    if inbox is not None:
        tools += _inbox_tools(inbox, wake_director)

    server = create_sdk_mcp_server(SERVER_NAME, tools=tools)
    return {SERVER_NAME: server}, [tool_name(t.name) for t in tools]


def _control_tools(controls: Controls, turn: TurnContext) -> list:
    """Everything {owner} can ask for in plain language instead of slash commands."""

    @tool(
        "pause_messages",
        "Pausa agentens egna utskick när {owner} ber om det (t.ex. 'var tyst i två timmar', 'stör mig inte nu', "
        "'bara viktiga saker ikväll'). quiet = inga utskick alls, focus = bara sådant som får störa. minutes=0 avslutar. "
        "Chatten påverkas aldrig.",
        {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["quiet", "focus"]},
                "minutes": {"type": "integer", "minimum": 0, "maximum": 10080},
            },
            "required": ["mode", "minutes"],
        },
    )
    async def pause_messages(args: dict[str, Any]) -> dict[str, Any]:
        return _text(await controls.pause(args["mode"], args["minutes"]))

    @tool(
        "get_shelf",
        "Hämta det som ligger på hyllan: bra saker du har hittat men inte stört {owner} med. Använd när {owner} frågar "
        "'något nytt?', 'vad har du hittat?' eller liknande. Det som hämtas tas bort från hyllan, så återge allt för {owner}.",
        {"type": "object", "properties": {}},
    )
    async def get_shelf(args: dict[str, Any]) -> dict[str, Any]:
        text, _ = await controls.shelf()
        return _text(text)

    @tool(
        "explain_message",
        "Förklara varför du skickade ett meddelande på eget initiativ: beslut, motivering och bedömning. "
        "target=replied om {owner} svarar på meddelandet, annars last_sent.",
        {"type": "object", "properties": {"target": {"type": "string", "enum": ["replied", "last_sent"]}}, "required": ["target"]},
    )
    async def explain_message(args: dict[str, Any]) -> dict[str, Any]:
        mid = turn.replied_to_message_id if args["target"] == "replied" else await controls.last_proactive_message_id()
        return _text(await controls.why(mid))

    @tool(
        "source_overview",
        "Visa vad du bevakar och hur ofta, och vad det har gett. Med `source` (t.ex. 'machinarium', 'devpost', 'jobtech') "
        "listas de senaste sakerna från den källan och vad som hände med var och en, och varför.",
        {"type": "object", "properties": {"source": {"type": "string"}}},
    )
    async def source_overview(args: dict[str, Any]) -> dict[str, Any]:
        if args.get("source"):
            return _text(await controls.recent_items(args["source"]))
        return _text(await controls.sources())

    @tool(
        "run_research",
        "Starta en schemalagd undersökning direkt när {owner} ber om det (t.ex. 'kolla efter nya hackathons nu'). "
        "Den körs i bakgrunden och {owner} får besked när den är klar.",
        {"type": "object", "properties": {"research_id": {"type": "string", "enum": controls.research_ids() or ["-"]}}, "required": ["research_id"]},
    )
    async def run_research(args: dict[str, Any]) -> dict[str, Any]:
        return _text(await controls.start_research(args["research_id"]))

    @tool(
        "activity_report",
        "Vad agenten har gjort idag och om allt fungerar: hälsa, händelser från sinnena, dispatcherns beslut, undersökningar "
        "och vad som skickats. Använd när {owner} undrar om du fungerar, varför du varit tyst eller vad du gjort.",
        {"type": "object", "properties": {}},
    )
    async def activity_report(args: dict[str, Any]) -> dict[str, Any]:
        from agent.inspect import Inspector

        return _text(await Inspector(controls.db).summary())

    @tool(
        "how_i_work",
        "Hur du (Audax) fungerar: delarna, när de kör, flödet från källa till meddelande, gränser, integritet, och vad du "
        "bevakar just nu. Använd när {owner} vill förstå hur du fungerar eller varför något händer som det gör.",
        {"type": "object", "properties": {}},
    )
    async def how_i_work(args: dict[str, Any]) -> dict[str, Any]:
        from agent.prompts import prompt

        return _text(prompt("self") + "\n\n## Just nu\n" + await controls.sources())

    return [pause_messages, get_shelf, explain_message, source_overview, run_research, activity_report, how_i_work]


def _inbox_tools(inbox: Inbox, wake_director=None) -> list:
    """Routing between the one voice and the two minds behind it (docs/ARCHITECTURE.md)."""

    @tool(
        "route_note",
        "Skicka något {owner} sagt vidare till rätt del av agenten, med {owner_s} EXAKTA ord. "
        "director: mål, projekt, vad {owner} lägger tid på, vad som skaver, idéer, vad som är viktigt i livet. "
        "manager: hur agenten upplevs (för mycket/lite, timing, längd, ton, vad som var bra). "
        "Använd också när {owner} svarar på en öppen fråga (ange request_id). Fakta om {owner} sparar du dessutom i minnet som vanligt.",
        {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "enum": ["director", "manager"]},
                "verbatim": {"type": "string", "description": "{owner_s} exakta ord, ordagrant citerade"},
                "interpretation": {"type": "string", "description": "Din tolkning, kort"},
                "request_id": {"type": ["integer", "null"]},
                "act_now": {
                    "type": "boolean",
                    "description": "Väck mottagaren nu i stället för i natt. director: {owner} vill att något ska drivas nu "
                    "(t.ex. 'hjälp mig hitta en praktikplats'). manager: tydlig och viktig feedback om upplevelsen som "
                    "inte bör vänta (t.ex. 'det här blir för mycket', 'sluta skriva så långt').",
                },
            },
            "required": ["owner", "verbatim", "interpretation"],
        },
    )
    async def route_note(args: dict[str, Any]) -> dict[str, Any]:
        await inbox.note(args["owner"], args["verbatim"], args["interpretation"], args.get("request_id"))
        if args.get("act_now") and wake_director:
            wake_director(args["owner"])
            return _text(personalize(
                "[internal] Saved. Work on this starts in the background within a minute: a thread, searches and "
                "watching as needed. Confirm briefly to {owner} that you are on it."
            ))
        return _text("[internal] Saved.")

    @tool(
        "watch_source",
        "Börja bevaka en källa permanent när {owner} ber om det: en sida (t.ex. en eventkalender), ett RSS-flöde, "
        "ett företags alla jobbannonser (ange karriärsidan), eller ett ämne/en händelse att följa i nyheterna "
        "(type=topic, url=sökfrågan, t.ex. 'KTH Innovation' eller 'Claude Code').",
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["page", "feed", "company", "topic"]},
                "name": {"type": "string", "description": "kort namn, eller företagsnamn"},
                "url": {"type": "string", "description": "URL, eller för topic: sökfrågan"},
                "lang": {"type": "string", "enum": ["sv", "en"], "description": "topic: nyheter på svenska eller engelska"},
                "note": {"type": "string", "description": "vad som är värdefullt från källan för {owner}"},
            },
            "required": ["type", "name", "url"],
        },
    )
    async def watch_source(args: dict[str, Any]) -> dict[str, Any]:
        from agent import direction_ops as ops

        if args["type"] == "company":
            result = await ops.add_company(args["name"], args["url"])
        elif args["type"] == "topic":
            result = ops.add_topic(args["name"], args["url"], args.get("lang", "en"), args.get("note", ""))
            result += " " + await ops.verify_source("topic", args["url"], args.get("lang", "en"))
        else:
            kind = "rss" if args["type"] == "feed" else "page"
            result = ops.add_page(args["name"], args["url"], args.get("note", ""), "event", kind)
            result += " " + await ops.verify_source(kind, args["url"])
        await inbox.note("director", f"[bad om bevakning] {args['name']}: {args['url']}", f"Ägaren lade till en {args['type']}-bevakning: {result}")
        return _text(result)

    @tool(
        "change_watch",
        "Ändra en befintlig bevakning när {owner} ber om det: stäng av den (stop) eller ändra hur ofta den kollas (pace). "
        "Ange källans id eller namn (se source_overview). Gäller direkt.",
        {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "id, t.ex. hn-best eller topic-machinarium-2"},
                "action": {"type": "string", "enum": ["stop", "pace"]},
                "minutes": {"type": "integer", "description": "pace: hur många minuter mellan kontrollerna"},
                "why": {"type": "string", "description": "{owner_s} skäl, med hens ord"},
            },
            "required": ["source", "action"],
        },
    )
    async def change_watch(args: dict[str, Any]) -> dict[str, Any]:
        from agent import direction_ops as ops
        from agent.sources.streams import load_stream_configs

        ids = [c.id for c in load_stream_configs()]
        sid = args["source"] if args["source"] in ids else next((i for i in ids if args["source"].lower() in i), None)
        if sid is None:
            return _text(f"Ingen bevakning matchar {args['source']}. Finns: {', '.join(ids)}.")
        if args["action"] == "stop":
            result = ops.disable_sense(sid, args.get("why", ""))
        elif args.get("minutes"):
            result = ops.tune_sense(sid, args["minutes"], args.get("why", ""))
        else:
            return _text("Ange hur många minuter mellan kontrollerna.")
        await inbox.note("director", args.get("why") or f"[bad om ändring] {sid}: {args['action']}", f"Ägaren ändrade bevakningen {sid}: {result}")
        return _text(result.replace("avstängt", "avstängd, inget mer därifrån").replace(": var", " kollas nu var"))

    @tool(
        "mark_request_asked",
        "Anropa när du har ställt en öppen fråga (från Director eller Manager) till {owner}.",
        {"type": "object", "properties": {"request_id": {"type": "integer"}}, "required": ["request_id"]},
    )
    async def mark_request_asked(args: dict[str, Any]) -> dict[str, Any]:
        await inbox.mark_asked(args["request_id"])
        return _text("[internal] Noted.")

    return [route_note, mark_request_asked, watch_source, change_watch]


def build_github_tools(reader) -> tuple[dict[str, Any], list[str]]:
    """Read-only GitHub, as its own MCP server so both the chat and the Director can use it."""

    @tool(
        "github",
        "Läs {owner_s} GitHub (bara läsning, även privata repon). action=repos listar alla repon. action=repo visar ett repo "
        "(filer i roten, senaste commits, README). action=read läser en fil eller katalog (path). Använd för att förstå vad "
        "{owner} bygger och hur långt det kommit, t.ex. när hen frågar om ett projekt.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["repos", "repo", "read"]},
                "repo": {"type": "string", "description": "reponamn, t.ex. Spra-kprogrammet"},
                "path": {"type": "string", "description": "fil eller katalog i repot (action=read)"},
            },
            "required": ["action"],
        },
    )
    async def github(args: dict[str, Any]) -> dict[str, Any]:
        try:
            if args["action"] == "repos":
                return _text(await reader.repos())
            if not args.get("repo"):
                return _text("Ange repo.")
            if args["action"] == "repo":
                return _text(await reader.repo(args["repo"]))
            return _text(await reader.read(args["repo"], args.get("path", "")))
        except Exception as e:
            return _text(f"GitHub gick inte att läsa just nu: {type(e).__name__}")

    server = create_sdk_mcp_server("github", tools=[github])
    return {"github": server}, ["mcp__github__github"]


def build_web_tools(web) -> tuple[dict[str, Any], list[str]]:
    """web_read: reliable page reading with fallbacks (agent/web.py). Replaces the built-in WebFetch."""

    @tool(
        "web_read",
        "Läs en webbsida. Pålitligare än vanlig hämtning: använder källans API när det finns (Devpost, GitHub, Lever m.fl.), "
        "annars en vanlig hämtning, och om sidan blockerar en läsartjänst. Ge inte upp efter ett fel: läs gärna en annan sida "
        "från samma källa eller sök vidare.",
        {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    )
    async def web_read(args: dict[str, Any]) -> dict[str, Any]:
        text, method = await web.read(args["url"])
        return _text(f"[{method}]\n{text}")

    return {"web": create_sdk_mcp_server("web", tools=[web_read])}, ["mcp__web__web_read"]


def build_mail_tools(reader) -> tuple[dict[str, Any], list[str]]:
    """Read-only mail (agent/mail.py) for the chat and Director. There is deliberately nothing that sends or changes."""
    from agent.mail import DATA_NOTE, format_line

    @tool(
        "mail_search",
        "Search {owner_s} mail (read-only; nothing can be sent or changed). Uses Gmail search syntax, e.g. "
        "'from:spotify.com', 'subject:intervju newer_than:30d', 'in:sent ansökan'. Returns id, date, sender, subject "
        "and a snippet, newest first. Use it when {owner} asks about something that would be in their mail.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "limit": {"type": "integer", "description": "max results, default 10"},
            },
            "required": ["query"],
        },
    )
    async def mail_search(args: dict[str, Any]) -> dict[str, Any]:
        try:
            found = await reader.search(args["query"], int(args.get("limit") or 10))
        except Exception as e:
            log.warning("mail_search failed: %s", e)
            return _text(f"[internal] The mail couldn't be read right now ({type(e).__name__}).")
        return _text(DATA_NOTE + "\n" + ("\n".join(format_line(m) for m in found) or "No matching mail."))

    @tool(
        "mail_read",
        "Read one mail in full (read-only), by the id from mail_search.",
        {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    )
    async def mail_read(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _text(DATA_NOTE + "\n" + await reader.read(args["id"]))
        except Exception as e:
            log.warning("mail_read failed: %s", e)
            return _text(f"[internal] That mail couldn't be read ({type(e).__name__}).")

    server = create_sdk_mcp_server("mail", tools=[mail_search, mail_read])
    return {"mail": server}, ["mcp__mail__mail_search", "mcp__mail__mail_read"]
