"""In-process MCP tools that Claude can call. Exposed as mcp__agent__<name>."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server as _create_sdk_mcp_server
from claude_agent_sdk import tool

from agent.db import Database
from agent.forms import AREAS
from agent.prompts import personalize

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent.builder import Builder
    from agent.controls import Controls
    from agent.posts import Posts
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
    # Isolation (docs/OPERATIONS.md, Säkerhet): once a run has read mail, it may not reach new addresses on the web,
    # so nothing written by a stranger in a mail can make Audax carry private data out in a URL.
    mail_read: bool = False
    refs: set = field(default_factory=set)  # IDs this turn touched (#P, #S, #F), linked to the messages afterwards
    message_row_id: int | None = None  # messages.id of the owner's latest message in this turn: the source of what they said


ISOLATED = (
    "[internal] Not available after reading mail in this turn: a run that has read mail can't reach new web "
    "addresses, so the mail can't leak. Answer from what you have; {owner} can ask again in a new message."
)


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
    inbox: Posts | None = None,
    memory_path=None,
) -> tuple[dict[str, Any], list[str]]:
    """Returns (mcp_servers entry, allowed tool names)."""

    @tool(
        "record_feedback",
        "Record {owner_s} feedback on something you sent or on how you behave ('mer sånt', 'det där var onödigt', "
        "'skriv kortare', 'varför sa du inte det här?'). Call it whenever {owner} expresses a view on content, timing, "
        "amount or tone.",
        {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["replied", "last_sent", "general"],
                    "description": "replied = the message {owner} replied to, last_sent = your latest message, general = your behaviour overall",
                },
                "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral", "missed"]},
                "aspect": {"type": "string", "enum": ["content", "timing", "amount", "tone", "other"]},
                "note": {"type": "string", "description": "Short summary of the feedback, in {owner_s} own words where possible"},
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

        from agent.pursuits import candidates_of_message

        candidate_id = next(iter(await candidates_of_message(db, message_id)), None)

        await db.execute(
            "INSERT INTO feedback (message_id, candidate_id, signal, value, note) VALUES (?, ?, 'text', ?, ?)",
            (message_id, candidate_id, f"{args['sentiment']}:{args['aspect']}", args["note"]),
        )
        return _text("[internal] Saved.")

    @tool(
        "interview_guide",
        "The guide for getting to know {owner} through a short interview. Use it when {owner} asks you to get to know "
        "them (again), e.g. 'intervjua mig', 'ställ frågor så du lär känna mig'.",
        {"type": "object", "properties": {}},
    )
    async def interview_guide(args: dict[str, Any]) -> dict[str, Any]:
        from agent.prompts import prompt

        return _text("[internal] Follow this guide from now on:\n\n" + prompt("interview"))

    @tool(
        "report_problem",
        "Report a fault in Audax itself to its maintainer: something that went wrong in how you work (a message sent "
        "twice, a source that keeps failing, a tool that errors). It shows up in the health check. Use it instead of "
        "request_change for faults.",
        {
            "type": "object",
            "properties": {"description": {"type": "string", "description": "What happened, when, and what you saw in the logs"}},
            "required": ["description"],
        },
    )
    async def report_problem(args: dict[str, Any]) -> dict[str, Any]:
        await db.execute("INSERT INTO problems (description) VALUES (?)", (args["description"],))
        return _text("[internal] Reported to the maintainer.")

    @tool(
        "track_pursuit",
        "Keep track of what {owner} goes for (#S): when they show interest in an opportunity, start preparing, apply, hear "
        "back, or decide against it; a project they start or stop; a goal they drop, pause or reach. target=replied links "
        "it to the message they replied to (a tip you sent), last_sent to your latest message; or give title (and "
        "organization, url, deadline) yourself. To update one, give its id from the list in your context. New goals and "
        "what they mean are Director's: pass {owner_s} words on with send_post. Set remind_at when {owner} asks to be "
        "reminded on a given day; otherwise the system queues one nudge shortly before the deadline, and the dispatcher "
        "picks the moment.",
        {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": ["replied", "last_sent", "none"]},
                "id": {"type": "integer"},
                "kind": {"type": "string", "enum": ["pursuit", "project", "goal"], "description": "pursuit (default): an opportunity; goal: only an existing one, by id"},
                "parent": {"type": "integer", "description": "The goal or project it serves (#S number), if clear"},
                "title": {"type": "string"},
                "organization": {"type": "string"},
                "url": {"type": "string"},
                "deadline": {"type": "string", "description": "YYYY-MM-DD"},
                "status": {"type": "string", "enum": ["interested", "preparing", "applied", "interviewing", "offer", "accepted", "rejected", "declined",
                                                      "active", "paused", "done", "dropped"],
                           "description": "An opportunity: interested … declined. A project or goal: active, paused, done, dropped"},
                "next_step": {"type": "string", "description": "Swedish, short"},
                "remind_at": {"type": "string", "description": "YYYY-MM-DD, only when {owner} asked for a reminder that day"},
                "note": {"type": "string", "description": "Swedish: what happened, in {owner_s} words where possible"},
            },
            "required": ["target"],
        },
    )
    async def track_pursuit(args: dict[str, Any]) -> dict[str, Any]:
        from agent.pursuits import KIND_LABEL, LABEL, Pursuits, candidates_of_message

        pursuits = Pursuits(db, schedule.settings.timezone if schedule is not None else "Europe/Stockholm")
        fields = {k: args.get(k) for k in ("id", "kind", "parent", "title", "organization", "url", "deadline", "status", "next_step", "remind_at", "note")}
        if fields.get("kind") == "goal" and not fields.get("id"):
            return _text("[internal] A new goal is Director's: pass " + personalize("{owner_s}") + " words on with send_post.")
        if args["target"] in ("replied", "last_sent") and not args.get("id"):
            mid = turn.replied_to_message_id
            if args["target"] == "last_sent":
                row = await db.fetchone("SELECT id FROM messages WHERE direction = 'out' ORDER BY id DESC LIMIT 1")
                mid = row["id"] if row else None
            cids = await candidates_of_message(db, mid)
            if cids and not args.get("title"):
                pid = await pursuits.from_candidate(cids[0], "chat", args.get("status") or "interested")
                if pid is not None:
                    fields["id"] = pid
            elif cids:
                fields["candidate_id"] = cids[0]
        try:
            pid, created = await pursuits.upsert(fields, "chat")
        except ValueError:
            return _text("[internal] Give a title (or reply to the tip it is about).")
        from agent.items import Items

        row = await Items(db).get(pid)
        turn.refs.add(f"#S{pid}")
        return _text(
            f"[internal] {'Tracked' if created else 'Updated'} #S{pid} ({KIND_LABEL[row['kind']]}): {row['title']} · {LABEL.get(row['status'], row['status'])}"
            + (f" · deadline {row['deadline']}" if row.get("deadline") else " · no deadline known")
            + (f" · reminder {row['remind_at']}" if row.get("remind_at") else "")
        )

    @tool(
        "lookup",
        "Look things up instead of trying to remember: an ID (#P12 a post, #S3 a goal, project or something {owner} goes for, #F210 a "
        "finding) gives its details and history; a few words search posts, the #S records, findings and the conversation. Use "
        "it for 'vad lovade du om Google?', 'vad hände med X?', or before you answer about something from earlier. What the "
        "office is doing now or next: situation.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    async def lookup(args: dict[str, Any]) -> dict[str, Any]:
        from agent import refs

        return _text("[internal] " + await refs.lookup(db, args["query"]))

    @tool(
        "remember",
        "Keep something lasting {owner} said about themself: a preference, a fact, a value (school, interests, what they "
        "don't want, what may always interrupt, schedule and habits, how they want to be talked to, limits for Audax). It "
        "becomes a belief with their message as its source and lands in the profile. Use their own words in `quote`. "
        "If it changes something already held (see the profile's #U ids), list those in `replaces`; if they say something "
        "you hold is wrong, `retract` it. Something only for now (\"inte den här veckan\") gets `valid_until`. A hesitation "
        "(\"jag tror inte…\") is kept as such: confidence low, and it changes nothing else by itself. Say \"Noterat\" only "
        "after this succeeded.",
        {
            "type": "object",
            "properties": {
                "statement": {"type": "string", "description": "Swedish, one line, about {owner} (\"Vill inte jobba med försvar\")"},
                "kind": {"type": "string", "enum": ["preference", "fact", "value"]},
                "area": {"type": "string", "enum": list(AREAS)},
                "subject": {"type": "string", "description": "what it is about in a few words, so related beliefs are found"},
                "quote": {"type": "string", "description": "{owner_s} own words"},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "bubble": {"type": "string", "enum": ["excludes", "maybe_limit"],
                           "description": "excludes: they ruled it out; maybe_limit: sounds like uncertainty rather than what they want"},
                "scope": {"type": "string", "description": "Swedish: when it holds, if not always"},
                "valid_until": {"type": "string", "description": "YYYY-MM-DD, for something temporary"},
                "replaces": {"type": "array", "items": {"type": "string"}, "description": "#U ids this replaces"},
                "retract": {"type": "string", "description": "a #U id {owner} says is wrong; nothing else needed"},
            },
        },
    )
    async def remember(args: dict[str, Any]) -> dict[str, Any]:
        from agent.beliefs import current
        from agent.forms import FormError
        from agent.items import Items, parse_ref

        items = Items(db)
        try:
            if args.get("retract"):
                n = parse_ref(args["retract"])
                await items.update(n, "assistant", status="retracted", note=f"{personalize('{owner}')} sa att det inte stämmer"
                                   + (f": \"{args['quote']}\"" if args.get("quote") else ""))
                turn.refs.add(f"#U{n}")
                if not args.get("statement"):
                    return _text(f"[internal] #U{n} retracted.")
            source = {"type": "owner", "date": __import__("datetime").date.today().isoformat()}
            if args.get("quote"):
                source["quote"] = args["quote"]
            if turn.message_row_id:
                source["message"] = turn.message_row_id
            data = {k: args.get(k) for k in ("area", "subject", "confidence", "bubble", "scope") if args.get(k)}
            new = await items.create("belief", args.get("statement") or "", "assistant", kind=args.get("kind") or "preference",
                                     data=data, source=source, valid_until=args.get("valid_until"))
            replaced = []
            for r in args.get("replaces") or []:
                if (old := parse_ref(r)) and old != new:
                    await items.update(old, "assistant", status="superseded", note=f"ersatt av #U{new}")
                    await items.link(new, old, "supersedes", "assistant")
                    replaced.append(f"#U{old}")
        except FormError as e:
            return _text(f"[internal] Not kept: {e}")
        turn.refs.add(f"#U{new}")
        others = [b for b in await current(db, subject=args["subject"]) if b["id"] != new] if args.get("subject") else []
        return _text(
            f"[internal] Kept as #U{new}" + (f", replacing {', '.join(replaced)}" if replaced else "") + "."
            + ("\nOther current beliefs about the same thing; if the new one changes them, call remember again with replaces:\n"
               + "\n".join(f"#U{b['id']} {b['title']}" for b in others) if others else "")
        )

    tools = [record_feedback, interview_guide, report_problem, track_pursuit, lookup, remember]

    if schedule is not None:

        @tool(
            "get_schedule",
            "{owner_s} schedule (timetable and own calendar) for the coming days. [?] = not yet known whether the "
            "session applies to {owner}, [ibland] = applies sometimes. Each session has a series id.",
            {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 21, "description": "Number of days from today"},
                    "include_not_mine": {"type": "boolean", "description": "Include sessions {owner} has said don't apply to them"},
                },
                "required": ["days"],
            },
        )
        async def get_schedule(args: dict[str, Any]) -> dict[str, Any]:
            return _text(await schedule.format_days(args["days"], args.get("include_not_mine", False)))

        @tool(
            "set_series_relevance",
            "Record whether a series of sessions (e.g. all exercise sessions in a course) applies to {owner}. Use it when "
            "{owner} says they do or don't attend something. The series id comes from get_schedule.",
            {
                "type": "object",
                "properties": {
                    "series_id": {"type": "integer"},
                    "relevance": {"type": "string", "enum": ["mine", "not_mine", "sometimes", "unknown"]},
                    "note": {"type": "string", "description": "Optional comment, e.g. 'bara udda veckor'"},
                },
                "required": ["series_id", "relevance"],
            },
        )
        async def set_series_relevance(args: dict[str, Any]) -> dict[str, Any]:
            ok = await schedule.set_relevance(args["series_id"], args["relevance"], args.get("note"))
            return _text("[internal] Saved." if ok else f"[internal] No series with id {args['series_id']}.")

        tools += [get_schedule, set_series_relevance]

    if controls is not None:
        tools += _control_tools(controls, turn, memory_path)

    if builder is not None:

        @tool(
            "request_change",
            "Change Audax itself when {owner} wants a new capability or different behaviour that settings and memory can't "
            "give. The change is built and tested on a separate branch, and {owner} approves it with a button before "
            "anything goes live. Describe the wish clearly and concretely, with {owner_s} own words and everything you know "
            "(links, examples). Not for preferences (memory) or for watching sources (watch_source).",
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short, in Swedish, e.g. 'Visa deadlines i schemat'"},
                    "description": {"type": "string"},
                },
                "required": ["title", "description"],
            },
        )
        async def request_change(args: dict[str, Any]) -> dict[str, Any]:
            try:
                cid = await builder.request(args["title"], args["description"])
            except RuntimeError as e:
                return _text(f"[internal] Can't start now: {e}. Tell " + personalize("{owner}") + ".")
            return _text(f"[internal] Change {cid} is being built; it takes a few minutes. " + personalize("{owner} gets a message with a summary and buttons when it is ready."))

        tools.append(request_change)

    if inbox is not None:
        tools += _post_tools(inbox, turn)

    server = create_sdk_mcp_server(SERVER_NAME, tools=tools)
    return {SERVER_NAME: server}, [tool_name(t.name) for t in tools]


def _control_tools(controls: Controls, turn: TurnContext, memory_path=None) -> list:
    """Everything {owner} can ask for in plain language instead of slash commands."""

    @tool(
        "pause_messages",
        "Pause Audax's proactive messages when {owner} asks ('var tyst i två timmar', 'stör mig inte nu', 'bara viktiga "
        "saker ikväll'). quiet = nothing proactive; focus = only what may interrupt. minutes=0 ends it. The chat itself "
        "is never affected.",
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
        "What you have saved for later: good things you found but didn't interrupt {owner} with. Use it when {owner} "
        "asks 'något nytt?', 'vad har du hittat?' or about something specific (`about`). Looking changes nothing: tell "
        "{owner} what answers their question, then call shelf_told with the ids you told them about.",
        {"type": "object", "properties": {"about": {"type": "string", "description": "Optional: only what matches, e.g. 'stories', 'praktik'"}}},
    )
    async def get_shelf(args: dict[str, Any]) -> dict[str, Any]:
        text, _ = await controls.shelf(args.get("about"))
        return _text(text)

    @tool(
        "shelf_told",
        "Mark saved items as told to {owner} (ids from get_shelf), so they aren't offered again.",
        {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ids"]},
    )
    async def shelf_told(args: dict[str, Any]) -> dict[str, Any]:
        n = await controls.dispatcher.outbox.mark_pulled([int(i) for i in args["ids"]])
        turn.refs.update(f"#F{int(i)}" for i in args["ids"])
        return _text(f"[internal] {n} marked as told.")

    @tool(
        "explain_message",
        "Why you sent a proactive message: the decision, its reason and the assessment. target=replied if {owner} is "
        "replying to that message, otherwise last_sent.",
        {"type": "object", "properties": {"target": {"type": "string", "enum": ["replied", "last_sent"]}}, "required": ["target"]},
    )
    async def explain_message(args: dict[str, Any]) -> dict[str, Any]:
        mid = turn.replied_to_message_id if args["target"] == "replied" else await controls.last_proactive_message_id()
        return _text(await controls.why(mid))

    @tool(
        "run_research",
        "Run one of your search missions now when {owner} asks ('kolla efter nya hackathons nu'). It runs in the "
        "background and {owner} hears back when it is done.",
        {"type": "object", "properties": {"research_id": {"type": "string", "enum": controls.research_ids() or ["-"]}}, "required": ["research_id"]},
    )
    async def run_research(args: dict[str, Any]) -> dict[str, Any]:
        return _text(await controls.start_research(args["research_id"]))

    agenda = controls.dispatcher.agenda

    @tool(
        "situation",
        "What the office is doing now and next: use it whenever {owner} asks what you or the roles are doing, when "
        "something will happen or how things stand, and before you say anything about that. about=now (default): what each role did last and has waiting, the planned "
        "steps with dates and who does them, what waits for the budget and why, what is booked and what was sent today. "
        "today: today's activity and health. agenda: what is planned to reach {owner} (`days`). sources: what you watch "
        "and what it yielded (`name` = one source or search mission: its latest items and what became of each). searches: "
        "how a search is done (`name` = a mission: its map, yield, coverage and gaps). how: how the office works.",
        {"type": "object", "properties": {
            "about": {"type": "string", "enum": ["now", "today", "agenda", "sources", "searches", "how"]},
            "name": {"type": "string"}, "days": {"type": "integer"}}},
    )
    async def situation(args: dict[str, Any]) -> dict[str, Any]:
        about, name = args.get("about") or "now", (args.get("name") or "").strip()
        if about == "today":
            from agent.inspect import Inspector

            text = await Inspector(controls.db).summary()
            if getattr(controls, "budget", None) is not None:
                text += "\n\n" + await controls.budget.words()
            return _text("[internal] " + text)
        if about == "agenda":
            return _text("[internal] Planned:\n" + await agenda.upcoming(int(args.get("days") or 7)))
        if about == "sources":
            return _text("[internal] " + (await controls.recent_items(name) if name else await controls.sources()))
        if about == "searches":
            return _text("[internal] " + await _searches(controls.db, memory_path, name))
        if about == "how":
            from agent.self_description import describe

            return _text("[internal] " + describe())
        from agent.situation import situation as now

        return _text("[internal] " + await now(controls.db, str(agenda.tz), getattr(controls, "budget", None), agenda))


    @tool(
        "book",
        "Book a contact with {owner} before you promise it, or move or cancel one. New: kind=reminder when {owner} asks to "
        "be reminded ('påminn mig fredag'): their own, fixed. kind=promise when you say you will come back ('jag hör av mig "
        "i morgon'); `latest` if it may move. A promise that waits for something rather than a time ('jag hör av mig när "
        "planen är klar'): `after` = the id it waits for (#T, #S, #E …); you are woken when it is done, and `when` is the "
        "latest you come back anyway (default a week). A new booking is checked at once: if something stands in the way "
        "(quiet hours, a session, a pause, a time that has passed) nothing is booked and you get the conflicts; tell {owner} "
        "and ask, and book again with confirm=true if they still want it. Only promise what is booked. Change: `id` (#B12, "
        "#F210 a planned finding, #T a promise that waits) with `when` to move it, or cancel=true.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["reminder", "promise"]},
                "when": {"type": "string", "description": "Local time, YYYY-MM-DD HH:MM"},
                "what": {"type": "string", "description": "Swedish: what to say then, as a brief for yourself"},
                "about": {"type": "string", "description": "Optional id it concerns, e.g. #S3"},
                "after": {"type": "string", "description": "promise: the id it waits for, instead of a time"},
                "latest": {"type": "string", "description": "promise: the latest acceptable time, YYYY-MM-DD HH:MM"},
                "confirm": {"type": "boolean", "description": "{owner} wants this time despite the conflicts"},
                "id": {"type": "string", "description": "change: what to move or cancel"},
                "cancel": {"type": "boolean"},
            },
        },
    )
    async def book(args: dict[str, Any]) -> dict[str, Any]:
        if args.get("id"):
            return _text("[internal] " + await _change_booking(controls, agenda, args))
        if not args.get("what") or not args.get("kind"):
            return _text("[internal] A new booking needs kind and what (or id to change one).")
        if args.get("after"):
            return _text("[internal] " + await _promise_after(controls.db, agenda, args, turn))
        at = agenda.parse(args.get("when") or "")
        if at is None:
            return _text("[internal] Give the time as YYYY-MM-DD HH:MM.")
        latest = agenda.parse(args["latest"]) if args.get("latest") else None
        bid, conflicts = await agenda.book(
            args["kind"], args["what"][:80], args["what"], at, "assistant", fixed=args["kind"] == "reminder",
            latest=latest, ref=args.get("about"), confirm=bool(args.get("confirm")),
        )
        if bid is None:
            return _text(personalize(
                "[internal] Not booked: " + "; ".join(conflicts) + ". Tell {owner} and suggest another time; if they still "
                "want this one, book again with confirm=true. Don't promise until it is booked."
            ))
        turn.refs.add(f"#B{bid}")
        return _text(f"[internal] Booked #B{bid}: {agenda.show(at)}" + (f" (despite: {'; '.join(conflicts)})" if conflicts else "") + ".")

    return [pause_messages, get_shelf, shelf_told, book, situation, explain_message, run_research]


async def _change_booking(controls: Controls, agenda, args: dict) -> str:
    from agent.items import Items, parse_ref

    ref = "#" + str(args["id"]).strip().lstrip("#").upper()
    if ref.startswith("#T"):   # a promise that waits for something
        n = parse_ref(ref)
        if args.get("cancel"):
            await Items(controls.db).update(n, "assistant", status="dropped", note="ägaren bad om det")
            return f"{ref} is cancelled."
        at = agenda.parse(args.get("when") or "")
        if at is None:
            return "Give the new time as YYYY-MM-DD HH:MM."
        await Items(controls.db).update(n, "assistant", review_at=f"{at:%Y-%m-%d %H:%M}", note="flyttad på ägarens begäran")
        return f"{ref} moved to {agenda.show(at)}."
    if args.get("cancel"):
        if ref.startswith("#F"):
            await controls.dispatcher.outbox.set_tier(int(ref[2:]), "shelf", "shelved")
            return f"{ref} is no longer planned; it is saved for later."
        return await agenda.cancel(int(ref[2:]), "assistant", "ägaren bad om det")
    at = agenda.parse(args.get("when") or "")
    if at is None:
        return "Give the new time as YYYY-MM-DD HH:MM."
    return await agenda.move(ref, at, "assistant", "ägaren bad om det")


async def _promise_after(db, agenda, args: dict, turn: TurnContext) -> str:
    """A promise that waits for something: a step of the Assistant's that the calendar wakes when what it waits for is
    done (agent/due.py, `follow`), or at the latest at `when`."""
    from datetime import datetime, timedelta

    from agent.forms import FormError
    from agent.items import Items, parse_ref

    items = Items(db)
    n = parse_ref(str(args["after"]))
    target = await items.get(n) if n else None
    if target is None:
        return "Nothing with that id to wait for; give the id of the step, goal or experiment (find it with situation or lookup)."
    at = agenda.parse(args.get("when") or "") or (datetime.now(agenda.tz) + timedelta(days=7))
    try:
        sid = await items.create("step", args["what"][:120], "assistant", owner="assistant", review_at=f"{at:%Y-%m-%d %H:%M}",
                                 data={"action": "tell_owner", "done_when": "Ägaren har fått veta", "after": ref_of(target)},
                                 note=f"löfte i samtalet: hör av mig när {ref_of(target)} är klart")
    except FormError as e:
        return f"Not booked: {e}"
    turn.refs.add(f"#T{sid}")
    return (f"Booked #T{sid}: you are woken when {ref_of(target)} ({target['title']}) is done, and come back {agenda.show(at)} at the "
            "latest. Promise only that.")


def ref_of(item: dict) -> str:
    from agent.items import ref

    return ref(item)


async def _searches(db, memory_path, name: str) -> str:
    """How a search is done: one mission's map, yield, coverage and gaps, or all missions in short."""
    from agent import search_map
    from agent.research import load_research

    missions = [t for t in load_research() if t.enabled]
    if name:
        match = next((t for t in missions if name.lower() in t.id.lower()), None)
        if match is None:
            return "No such mission. Missions: " + ", ".join(t.id for t in missions)
        if memory_path is None:
            return f"Goal: {match.goal}"
        return f"Goal: {match.goal}\n" + await search_map.overview(db, memory_path, match.id)
    parts = []
    for t in missions:
        st = await search_map.mission_stats(db, t.id)
        parts.append(f"- {t.id}: {t.goal[:140]} · {st['runs_30d']} runs/30 d, kept in total {st['kept_total']}")
    return "Search missions:\n" + "\n".join(parts)


def _post_tools(posts: Posts, turn: TurnContext | None = None) -> list:
    """The internal mail (docs/MODEL.md, "Postgången"): the Assistant passes on what the owner says and asks the office's questions."""

    @tool(
        "send_post",
        "Pass something {owner} said to the role it concerns, with their exact words and your reading. director: goals, "
        "projects, what {owner} spends time on, what chafes, ideas, what matters in their life. manager: how Audax feels "
        "to them (too much or too little, timing, length, tone, what was good). Facts about {owner} still go into memory "
        "as usual; answers to the office's open questions go with `question`.",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string", "enum": ["director", "manager"], "description": "Not needed with answer_to"},
                "verbatim": {"type": "string", "description": "{owner_s} exact words, quoted verbatim"},
                "interpretation": {"type": "string", "description": "Your reading, briefly"},
                "wake": {
                    "type": "boolean",
                    "description": "Start the recipient's work now instead of tonight. director: {owner} wants something driven "
                    "now ('hjälp mig hitta en praktikplats'). manager: clear, important feedback on the experience that "
                    "shouldn't wait ('det här blir för mycket', 'sluta skriva så långt').",
                },
            },
            "required": ["verbatim", "interpretation"],
        },
    )
    async def send_post(args: dict[str, Any]) -> dict[str, Any]:
        answer_to, to = None, args.get("to")
        if args.get("answer_to"):
            m = re.search(r"\d+", str(args["answer_to"]))
            q = await posts.get(int(m.group())) if m else None
            if q is None:
                return _text("[internal] No such question; give its #P id from the open question.")
            answer_to, to = q["id"], q["sender"]
        if to not in ("director", "manager"):
            return _text("[internal] Give `to` (director or manager), or answer_to.")
        pid = await posts.send(
            "assistant", to, "answer" if answer_to else "note", args["interpretation"], verbatim=args["verbatim"],
            answer_to=answer_to, from_owner=True, wake=bool(args.get("wake")),
        )
        if turn is not None:
            turn.refs.add(f"#P{pid}")
        if args.get("wake"):
            return _text(personalize(
                f"[internal] Sent (#P{pid}), and acted on now. How it is routed is internal. Tell {{owner}} the outcome as "
                "your own commitment, e.g. what you will look for and that you will report back."
            ))
        return _text(f"[internal] Sent (#P{pid}).")

    @tool(
        "question",
        "An open question from the office (#Q, or #P): asked=true once you have asked {owner} it; when they answer, "
        "`answer` with their own words, and it goes back to the role that asked. If they clearly don't want to answer, "
        "declined=true with what they said.",
        {"type": "object", "properties": {"id": {"type": "string"}, "asked": {"type": "boolean"}, "answer": {"type": "string"},
                                          "declined": {"type": "boolean"}}, "required": ["id"]},
    )
    async def question(args: dict[str, Any]) -> dict[str, Any]:
        from datetime import datetime

        from agent.forms import FormError
        from agent.items import Items, parse_ref

        raw = str(args["id"]).strip().lstrip("#").upper()
        m = re.search(r"\d+", raw)
        if not m:
            return _text("[internal] Give the question's id.")
        n = int(m.group())
        if raw.startswith("P"):   # a post from a role: the answer goes back as a post
            if args.get("answer"):
                q = await posts.get(n)
                if q is None:
                    return _text("[internal] No such question.")
                pid = await posts.send("assistant", q["sender"], "answer", args["answer"], verbatim=args["answer"],
                                       answer_to=n, from_owner=True)
                if turn is not None:
                    turn.refs.add(f"#P{pid}")
                return _text(f"[internal] The answer went back to {q['sender']}.")
            await posts.asked(n)
            return _text("[internal] Noted.")
        items = Items(posts.db)
        q = await items.get(n)
        if not q or q["form"] != "question":
            return _text("[internal] No such question.")
        try:
            if args.get("answer") or args.get("declined"):
                await items.update(n, "assistant", status="unanswered" if args.get("declined") else "answered",
                                   data={"answer": args.get("answer") or "ville inte svara"}, note="svar från ägaren")
            else:
                await items.update(n, "assistant", status="asked", data={"asked_at": datetime.now().isoformat(timespec="minutes")},
                                   note="ställd i samtalet")
                return _text("[internal] Noted.")
        except FormError as e:
            return _text(f"[internal] {e}")
        if turn is not None:
            turn.refs.add(f"#Q{n}")
        waker = type(posts).waker
        if q["owner"] in ("director", "manager", "researcher") and waker is not None:
            waker(q["owner"])   # the owner's own words: the one who asked may hear it now
        return _text(f"[internal] The answer went back to {q['owner']}.")

    @tool(
        "watch_source",
        "Start watching a source permanently when {owner} asks: a page (e.g. an event calendar), an RSS feed, all of a "
        "company's job ads (give its careers page), or a topic or event to follow in the news (type=topic, url=the "
        "search query, e.g. 'KTH Innovation' or 'Claude Code'). The source is checked at once; tell {owner} the result.",
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["page", "feed", "company", "topic"]},
                "name": {"type": "string", "description": "short name, or the company name"},
                "url": {"type": "string", "description": "URL, or for topic: the search query"},
                "lang": {"type": "string", "enum": ["sv", "en"], "description": "topic: news in Swedish or English"},
                "note": {"type": "string", "description": "in Swedish: what is worth something to {owner} from this source"},
            },
            "required": ["type", "name", "url"],
        },
    )
    async def watch_source(args: dict[str, Any]) -> dict[str, Any]:
        from agent import direction_ops as ops

        if turn is not None and turn.mail_read and args["type"] != "topic":
            return _text(personalize(ISOLATED))

        if args["type"] == "company":
            result = await ops.add_company(posts.db, args["name"], args["url"])
        elif args["type"] == "topic":   # the owner asked for it: what comes is told at once (urgent)
            result = await ops.add_topic(posts.db, args["name"], args["url"], args.get("lang", "en"), args.get("note", ""), urgent=True, by="assistant")
            result += " " + await ops.verify_source("topic", args["url"], args.get("lang", "en"))
        else:
            kind = "rss" if args["type"] == "feed" else "page"
            result = await ops.add_page(posts.db, args["name"], args["url"], args.get("note", ""), "event", kind, urgent=True, by="assistant")
            result += " " + await ops.verify_source(kind, args["url"])
        await posts.send("assistant", "researcher", "note", f"Ägaren lade till en {args['type']}-bevakning: {result}",
                         verbatim=f"[bad om bevakning] {args['name']}: {args['url']}", from_owner=True)
        return _text(result)

    @tool(
        "change_watch",
        "Change an existing watch when {owner} asks: switch it off (stop) or change how often it is checked (pace). Give "
        "the source's id or name (see source_overview). Takes effect immediately.",
        {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "id, e.g. hn-best or topic-machinarium-2"},
                "action": {"type": "string", "enum": ["stop", "pace"]},
                "minutes": {"type": "integer", "description": "pace: minutes between checks"},
                "why": {"type": "string", "description": "{owner_s} reason, in their words"},
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
            result = await ops.disable_sense(posts.db, sid, args.get("why", ""), by="assistant")
        elif args.get("minutes"):
            result = await ops.tune_sense(posts.db, sid, args["minutes"], args.get("why", ""), by="assistant")
        else:
            return _text("[internal] Give the number of minutes between checks.")
        await posts.send("assistant", "researcher", "note", f"Ägaren ändrade bevakningen {sid}: {result}",
                         verbatim=args.get("why") or f"[bad om ändring] {sid}: {args['action']}", from_owner=True)
        return _text(result.replace("avstängt", "avstängd, inget mer därifrån").replace(": var", " kollas nu var"))

    return [send_post, question, watch_source, change_watch]


def build_github_tools(reader) -> tuple[dict[str, Any], list[str]]:
    """Read-only GitHub, as its own MCP server so both the chat and the Director can use it."""

    @tool(
        "github",
        "Read {owner_s} GitHub (read-only, private repos included). action=repos lists all repos; action=repo shows one "
        "(root files, recent commits, README); action=read reads a file or directory (path). Use it to understand what "
        "{owner} builds and how far it has come, e.g. when they ask about a project.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["repos", "repo", "read"]},
                "repo": {"type": "string", "description": "repo name"},
                "path": {"type": "string", "description": "file or directory in the repo (action=read)"},
            },
            "required": ["action"],
        },
    )
    async def github(args: dict[str, Any]) -> dict[str, Any]:
        try:
            if args["action"] == "repos":
                return _text(await reader.repos())
            if not args.get("repo"):
                return _text("[internal] Give a repo.")
            if args["action"] == "repo":
                return _text(await reader.repo(args["repo"]))
            return _text(await reader.read(args["repo"], args.get("path", "")))
        except Exception as e:
            return _text(f"[internal] GitHub couldn't be read right now: {type(e).__name__}")

    server = create_sdk_mcp_server("github", tools=[github])
    return {"github": server}, ["mcp__github__github"]


def build_web_tools(web, turn: TurnContext | None = None) -> tuple[dict[str, Any], list[str]]:
    """web_read: reliable page reading with fallbacks (agent/web.py). Replaces the built-in WebFetch."""

    @tool(
        "web_read",
        "Read a web page. More reliable than a plain fetch: it uses the source's API where there is one (Devpost, GitHub, "
        "Lever and others), otherwise a direct fetch, and a reader service if the page blocks that. Don't give up after "
        "one failure: try another page from the same source, or search further.",
        {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    )
    async def web_read(args: dict[str, Any]) -> dict[str, Any]:
        if turn is not None and turn.mail_read:
            return _text(personalize(ISOLATED))
        text, method = await web.read(args["url"])
        return _text(f"[{method}]\n{text}")

    return {"web": create_sdk_mcp_server("web", tools=[web_read])}, ["mcp__web__web_read"]


def build_mail_tools(reader, turn: TurnContext | None = None) -> tuple[dict[str, Any], list[str]]:
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
        if turn is not None:
            turn.mail_read = True  # before reading: from here on this turn can't reach new web addresses
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
        if turn is not None:
            turn.mail_read = True
        try:
            return _text(DATA_NOTE + "\n" + await reader.read(args["id"]))
        except Exception as e:
            log.warning("mail_read failed: %s", e)
            return _text(f"[internal] That mail couldn't be read ({type(e).__name__}).")

    server = create_sdk_mcp_server("mail", tools=[mail_search, mail_read])
    return {"mail": server}, ["mcp__mail__mail_search", "mcp__mail__mail_read"]
