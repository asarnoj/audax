"""The office's tools (agent/forms.py, agent/items.py): how a role finds, reads, creates, changes and links items
during its run. Every write is checked at once against the form and the role's rights, and a problem goes straight
back to the role so it can put it right, instead of being found after the run, or never.

Exposed as mcp__office__<name>: forms, find, read, create, update, link.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import tool

from agent.db import Database
from agent.forms import CREATE, FORMS, FormError
from agent.items import Items, parse_ref, ref
from agent.tools import TurnContext, _text, create_sdk_mcp_server

SERVER = "office"
NAMES = [f"mcp__{SERVER}__{n}" for n in ("forms", "find", "read", "create", "update", "link")]


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


def build_office_tools(db: Database, role: str, turn: TurnContext | None = None) -> tuple[dict[str, Any], list[str]]:
    """Returns (mcp_servers entry, allowed tool names) for a role's run."""
    return {SERVER: create_sdk_mcp_server(SERVER, tools=office_tools(db, role, turn))}, NAMES


def office_tools(db: Database, role: str, turn: TurnContext | None = None) -> list:
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
        return _text(f"[internal] Created {Items.line(item)}")

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

    return [forms, find, read, create, update, link]


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
