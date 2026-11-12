"""The office's tools (agent/forms.py, agent/items.py): how a role finds, reads, creates, changes and links items
during its run. Every write is checked at once against the form and the role's rights, and a problem goes straight
back to the role so it can put it right, instead of being found after the run, or never.

Exposed as mcp__office__<name>: forms, find, read, create, update, link, find_out, follow (an item or a saved
question), answer (a point to decide, agent/docket.py), thread, and text (a section of a text the role owns,
agent/texts.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from agent.db import Database
from agent.forms import CREATE, FORMS, FormError
from agent.items import Items, parse_ref, ref
from agent.tools import TurnContext, _text, create_sdk_mcp_server

SERVER = "office"
NAMES = [f"mcp__{SERVER}__{n}" for n in ("forms", "find", "read", "create", "update", "link")]   # without text


def describe_forms(role: str | None = None) -> str:
    """The forms' templates, for a role's prompt or the forms tool: what each is for, its kinds, statuses and fields."""
    out = []
    for form in FORMS.values():
        if role and form.name not in CREATE.get(role, set()):
            continue
        kinds = " · ".join(f"{k}: {', '.join(v)}" for k, v in form.statuses.items() if k) or ", ".join(form.statuses[""])
        fields = "; ".join(f"{f.name}{'*' if f.required else ''} ({f.type}{': ' + ', '.join(f.choices) if f.choices else ''})"
                           + (f" {f.help}" if f.help else "") for f in form.fields)
        musts = [w for w, on in (("review_at*", form.needs_review), ("source*", form.needs_source)) if on]
        out.append(f"#{form.letter} {form.name}: {form.help}\n  kinds and statuses: {kinds}\n  fields: {fields}"
                   + (f"\n  also required: {', '.join(musts)}" if musts else ""))
    return "\n".join(out) + "\n(* required. source = {\"type\": owner|observed|inferred|external, plus what shows it: a message, a quote, a url})"


def build_office_tools(db: Database, role: str, turn: TurnContext | None = None, memory: Path | None = None) -> tuple[dict[str, Any], list[str]]:
    """Returns (mcp_servers entry, allowed tool names) for a role's run; with `memory`, also its texts."""
    tools = office_tools(db, role, turn, memory)
    return {SERVER: create_sdk_mcp_server(SERVER, tools=tools)}, [f"mcp__{SERVER}__{t.name}" for t in tools]


def office_tools(db: Database, role: str, turn: TurnContext | None = None, memory: Path | None = None) -> list:
    items = Items(db)

    def touched(item_id: int, form: str) -> None:
        if turn is not None:
            turn.refs.add(f"#{FORMS[form].letter}{item_id}")

    def item_id(value) -> int:
        n = parse_ref(str(value))
        if n is None:
            raise FormError(f"not an id: {value!r}")
        return n

    @tool("forms", "The forms you can create, with their kinds, statuses and fields.", {"type": "object", "properties": {}})
    async def forms(args: dict[str, Any]) -> dict[str, Any]:
        return _text(describe_forms(role))

    @tool(
        "find",
        "Find items: by form, kind, status, what they serve (parent), owner or words. open=true leaves out what is over. "
        "One line each; read one for everything about it.",
        {"type": "object", "properties": {
            "form": {"type": "string", "enum": list(FORMS)}, "kind": {"type": "string"}, "status": {"type": "string"},
            "open": {"type": "boolean"}, "parent": {"type": "string"}, "owner": {"type": "string"}, "words": {"type": "string"},
            "limit": {"type": "integer"}}},
    )
    async def find(args: dict[str, Any]) -> dict[str, Any]:
        try:
            found = await items.find(args.get("form"), kind=args.get("kind"), status=args.get("status"), open_only=bool(args.get("open")),
                                     parent=item_id(args["parent"]) if args.get("parent") else None, owner=args.get("owner"),
                                     text=args.get("words"), order="updated_at DESC", limit=min(int(args.get("limit") or 40), 100))
        except FormError as e:
            return _text(f"[internal] {e}")
        return _text("\n".join(Items.line(i) for i in found) or "[internal] Nothing found.")

    @tool("read", "Everything about one item: its fields, source, what it serves and holds, its links and its history.",
          {"type": "object", "properties": {"id": {"type": "string", "description": "e.g. #S5"}}, "required": ["id"]})
    async def read(args: dict[str, Any]) -> dict[str, Any]:
        try:
            text = await items.describe(item_id(args["id"]))
        except FormError as e:
            return _text(f"[internal] {e}")
        return _text(text or "[internal] No such item.")

    @tool(
        "create",
        "Create an item. Check the form's fields with the forms tool first. It is checked at once: if something is "
        "missing or wrong you get every problem back, fix it and try again.",
        {"type": "object", "properties": {
            "form": {"type": "string", "enum": list(FORMS)}, "kind": {"type": "string"}, "title": {"type": "string", "description": "Swedish"},
            "status": {"type": "string"}, "parent": {"type": "string", "description": "the id of what it serves"},
            "owner": {"type": "string", "description": "the role that does it (a step); default you"},
            "fields": {"type": "object", "description": "the form's own fields"}, "source": {"type": "object"},
            "review_at": {"type": "string", "description": "YYYY-MM-DD or YYYY-MM-DD HH:MM"}, "valid_until": {"type": "string"},
            "note": {"type": "string", "description": "Swedish: why"}},
         "required": ["form", "title"]},
    )
    async def create(args: dict[str, Any]) -> dict[str, Any]:
        try:
            new = await items.create(
                args["form"], args["title"], role, kind=args.get("kind"), status=args.get("status"), owner=args.get("owner"),
                parent=item_id(args["parent"]) if args.get("parent") else None, data=_obj(args.get("fields")), source=_obj(args.get("source")),
                review_at=args.get("review_at"), valid_until=args.get("valid_until"), note=args.get("note"), check_rights=True)
        except FormError as e:
            return _text(f"[internal] Not created: {e}")
        item = await items.get(new)
        touched(new, item["form"])
        from agent.lessons import tried_before

        before = await tried_before(db, item, role)   # what is like it and has been tried (omgång 16)
        return _text(f"[internal] Created {Items.line(item)}" + (f"\n\n{before}" if before else ""))

    @tool(
        "update",
        "Change an item: only what you give is changed, and the change is logged with your note. To end one, set a "
        "closing status (done, dropped, kept …) and say why in the note.",
        {"type": "object", "properties": {
            "id": {"type": "string"}, "title": {"type": "string"}, "status": {"type": "string"}, "kind": {"type": "string"},
            "parent": {"type": "string", "description": "the id of what it serves; 'none' to unlink"}, "owner": {"type": "string"},
            "fields": {"type": "object"}, "source": {"type": "object"}, "review_at": {"type": "string"}, "valid_until": {"type": "string"},
            "note": {"type": "string", "description": "Swedish: what changed and why"}},
         "required": ["id"]},
    )
    async def update(args: dict[str, Any]) -> dict[str, Any]:
        try:
            n = item_id(args["id"])
            parent = ... if "parent" not in args else (None if str(args["parent"]).lower() in ("none", "") else item_id(args["parent"]))
            changed = await items.update(
                n, role, title=args.get("title"), status=args.get("status"), kind=args.get("kind"), owner=args.get("owner"), parent=parent,
                data=_obj(args.get("fields")), source=_obj(args.get("source")), review_at=args.get("review_at"),
                valid_until=args.get("valid_until"), note=args.get("note"), check_rights=True)
        except FormError as e:
            return _text(f"[internal] Not changed: {e}")
        item = await items.get(n)
        touched(n, item["form"])
        return _text(f"[internal] {Items.line(item)}" + (f" (changed: {', '.join(changed)})" if changed else " (nothing changed)"))

    @tool(
        "link",
        "Link two items with a relation in a word or two: tests (an experiment tests a belief), answers, supersedes, about, "
        "blocks, came_from …",
        {"type": "object", "properties": {"from": {"type": "string"}, "to": {"type": "string"}, "relation": {"type": "string"}},
         "required": ["from", "to", "relation"]},
    )
    async def link(args: dict[str, Any]) -> dict[str, Any]:
        try:
            a, b = item_id(args["from"]), item_id(args["to"])
            await items.link(a, b, args["relation"].strip(), role)
        except FormError as e:
            return _text(f"[internal] Not linked: {e}")
        return _text(f"[internal] Linked {ref(await items.get(a))} —{args['relation']}→ {ref(await items.get(b))}")

    @tool(
        "find_out",
        "Something you don't know and that matters: register it, with your best guess, and choose where the answer comes "
        "from. ask=owner: the Assistant asks it in its own words when it fits. ask=world: Researcher finds out at once (a "
        "step). ask=self: you test it (then create an experiment with this question as its parent) or wait for a signal "
        "(give review_at). ask=director/manager/researcher: that role answers. The answer comes back to you, next to your "
        "guess; then say what it meant (update: fields.conclusion, status settled).",
        {"type": "object", "properties": {
            "what": {"type": "string", "description": "Swedish: the question, plainly"},
            "why": {"type": "string", "description": "Swedish: what hangs on it"},
            "guess": {"type": "string", "description": "Swedish: your best guess now, and how sure"},
            "settles_it": {"type": "string", "description": "Swedish: what would settle it"},
            "context": {"type": "string", "description": "Swedish, for the Assistant who asks: background that helps it ask well and follow up itself"},
            "ask": {"type": "string", "enum": ["owner", "world", "self", "director", "manager", "researcher"]},
            "kind": {"type": "string", "enum": ["clarify", "intent", "check", "followup", "challenge"]},
            "about": {"type": "string", "description": "the id of what it serves, e.g. #S5 or #U141"},
            "review_at": {"type": "string", "description": "YYYY-MM-DD: when you want the answer at the latest"}},
         "required": ["what", "why", "guess", "ask"]},
    )
    async def find_out(args: dict[str, Any]) -> dict[str, Any]:
        try:
            qid = await items.create(
                "question", args["what"], role, kind=args.get("kind") or "clarify", owner=role,
                parent=item_id(args["about"]) if args.get("about") else None, review_at=args.get("review_at"),
                data={"to": args["ask"], "why": args["why"], "guess": args.get("guess"), "settles_it": args.get("settles_it"),
                      "context": args.get("context")},
                note="vill veta", check_rights=True)
        except FormError as e:
            return _text(f"[internal] Not registered: {e}")
        touched(qid, "question")
        q = await items.get(qid)
        then = {"owner": "The Assistant asks it when it fits.", "world": "Researcher is on it now.",
                "self": "Now create the experiment with this as its parent, or wait for the signal.",
                }.get(args["ask"], f"{args['ask'].capitalize()} sees it in its review.")
        return _text(f"[internal] {Items.line(q)}. {then}")

    @tool(
        "follow",
        "Keep an eye on something; it is what you carry from run to run. Either an item (id): anything you judge "
        "important for your work, another role's too. Say when you mean to look at it again and why; it comes up then, "
        "or earlier if something new happens in it (the owner mentions it, it changes, a deadline comes close); then "
        "read its thread and follow again with your next date, or stop=true. Or a saved question (name + question): "
        "what it finds is shown in each of your runs, e.g. {\"form\": \"opportunity\", \"open\": true, \"within_days\": 21}. "
        "A question takes form, kind, status, open, owner, words, within_days (a deadline, date or review day that "
        "close) and limit (at most 15). You see at once what it finds. stop=true with the name ends it.",
        {"type": "object", "properties": {
            "id": {"type": "string"}, "when": {"type": "string", "description": "YYYY-MM-DD (or YYYY-MM-DD HH:MM)"},
            "name": {"type": "string", "description": "a saved question's short name"},
            "question": {"type": "object", "description": "a saved question"},
            "why": {"type": "string", "description": "Swedish: why you follow it, and what to look for next time"},
            "stop": {"type": "boolean"}},
         "required": ["why"]},
    )
    async def follow(args: dict[str, Any]) -> dict[str, Any]:
        from agent import threads

        if args.get("name") and not args.get("id"):
            try:
                if args.get("stop"):
                    ok = await threads.stop_question(db, role, args["name"], args["why"])
                    return _text(f"[internal] {'Stopped following' if ok else 'You did not follow'} the question {args['name']}.")
                found = await threads.follow_question(db, role, args["name"], _obj(args.get("question")) or {}, args["why"])
            except (ValueError, FormError) as e:
                return _text(f"[internal] Not saved: {e}")
            return _text(f"[internal] Following the question {args['name']}. It finds now:\n{found}")
        try:
            if not args.get("id"):
                raise FormError("give an id, or a name and a question")
            n = item_id(args["id"])
            item = await items.get(n)
            if item is None:
                raise FormError("no such item")
            if args.get("stop"):
                ok = await threads.unfollow(db, role, n, args["why"])
                return _text(f"[internal] {'Stopped following' if ok else 'You did not follow'} {ref(item)}.")
            if not args.get("when"):
                raise FormError("give when you mean to look at it again")
            await threads.follow(db, role, n, args["when"].strip(), args["why"])
        except FormError as e:
            return _text(f"[internal] Not followed: {e}")
        return _text(f"[internal] Following {ref(item)} until {args['when']}.")

    @tool(
        "answer",
        "Answer a point under 'Att avgöra nu' (D12). decision: done (handled), changed (you changed what it is about), "
        "kept (looked, it stays), struck (removed it), nothing (nothing is touched / nothing new), later (give `later`, "
        "the day it comes back, and why), lesson (give `lesson`: what it teaches about how to work, one or two "
        "sentences). The note says what you decided and why, in Swedish. Do the work first (update, create …), then answer.",
        {"type": "object", "properties": {
            "id": {"type": "string", "description": "e.g. D12"},
            "decision": {"type": "string", "enum": ["done", "changed", "kept", "struck", "nothing", "later", "lesson"]},
            "note": {"type": "string", "description": "Swedish: what you decided and why"},
            "later": {"type": "string", "description": "YYYY-MM-DD, with decision later"},
            "lesson": {"type": "string", "description": "Swedish, with decision lesson: the lesson, general enough to use next time"}},
         "required": ["id", "decision", "note"]},
    )
    async def answer(args: dict[str, Any]) -> dict[str, Any]:
        from agent import docket
        from agent.lessons import learn

        raw = str(args["id"]).strip().lstrip("#").upper().lstrip("D")
        if not raw.isdigit():
            return _text(f"[internal] Not answered: not a point: {args['id']!r} (points read D12)")
        try:
            if args["decision"] == "lesson" and not (args.get("lesson") or "").strip():
                raise ValueError("decision lesson needs the lesson itself")
            p = await docket.answer(db, role, int(raw), args["decision"], args.get("note") or "", args.get("later"))
        except ValueError as e:
            return _text(f"[internal] Not answered: {e}")
        extra = ""
        if args["decision"] == "lesson":
            lid = await learn(db, memory, role, args["lesson"], json.loads(p["refs"] or "[]"))
            extra = f" Lesson kept as #U{lid}."
        return _text(f"[internal] D{p['id']} answered ({args['decision']}).{extra}")

    @tool("thread", "Everything about one thing: its history, what the owner said about it, what hangs together with it, every time "
                    "it was brought up with the owner and how that landed, and what is coming. Read it before you decide what to ask.",
          {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
    async def thread(args: dict[str, Any]) -> dict[str, Any]:
        from agent import threads

        try:
            text = await threads.thread(db, item_id(args["id"]))
        except FormError as e:
            return _text(f"[internal] {e}")
        return _text(text or "[internal] No such item.")

    tools = [forms, find, read, create, update, link, find_out, follow, answer, thread]
    if memory is not None:
        from agent import texts

        @tool(
            "text",
            "Change one section (a '## heading') of a text you own: it replaces that section, or adds it if it isn't "
            "there; empty text removes it. The rest of the file stays as it is. Refer to items by id instead of copying "
            "their facts. Your texts:\n" + texts.describe(role),
            {"type": "object", "properties": {
                "file": {"type": "string", "description": "path under memory/, e.g. direction/now.md"},
                "section": {"type": "string", "description": "the heading, without ##"},
                "text": {"type": "string", "description": "Swedish: the section's new content, without its heading"}},
             "required": ["file", "section", "text"]},
        )
        async def text(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return _text("[internal] " + texts.edit(memory, role, args["file"], args["section"], args.get("text")))
            except texts.TextError as e:
                return _text(f"[internal] Not changed: {e}")

        tools.append(text)
    return tools


def _obj(value) -> dict | None:
    """Tools sometimes get an object as a JSON string."""
    if value in (None, "", {}):
        return None if value != {} else {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as e:
            raise FormError("fields and source must be objects") from e
    if not isinstance(value, dict):
        raise FormError("fields and source must be objects")
    return value
