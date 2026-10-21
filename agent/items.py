"""The office's items (agent/forms.py): one table for everything worth following over time, with one history and one
set of links, checked against the forms.

An item reads as a flat dict: the core columns plus its form's own fields (stored as JSON in `data`), and `source`
as a dict. Every change is logged with who made it and in which run (agent/trail.py), so an item's history answers
"when did this change, and why".
"""

from __future__ import annotations

import json
from datetime import date

from agent import trail
from agent.db import Database
from agent.forms import CORE, FORMS, LETTERS, FormError, check, may


def ref(item: dict) -> str:
    return f"#{FORMS[item['form']].letter}{item['id']}"


def parse_ref(text: str) -> int | None:
    """'#S5', 'S5', '#U31' or '31' -> 31; the letter must match a form when given."""
    t = text.strip().lstrip("#")
    if t[:1].isalpha():
        if t[:1].upper() not in LETTERS:
            return None
        t = t[1:]
    return int(t) if t.isdigit() else None


class Items:
    # Called with each created or changed item, e.g. to write the profile when a belief changes (agent/beliefs.py).
    watchers: list = []

    def __init__(self, db: Database):
        self.db = db

    async def _changed(self, item_id: int) -> None:
        if Items.watchers and (item := await self.get(item_id)):
            for watch in Items.watchers:
                await watch(self.db, item)

    # --- reading ------------------------------------------------------------------------------

    @staticmethod
    def _flat(row) -> dict:
        item = {k: row[k] for k in CORE}
        item["source"] = json.loads(row["source"]) if row["source"] else None
        for k, v in json.loads(row["data"] or "{}").items():
            if v is not None:
                item[k] = v
        return item

    async def get(self, item_id: int) -> dict | None:
        row = await self.db.fetchone("SELECT * FROM items WHERE id = ?", (item_id,))
        return self._flat(row) if row else None

    async def find(self, form: str | tuple | None = None, kind: str | tuple | None = None, status: str | tuple | None = None,
                   open_only: bool = False, parent: int | None = None, owner: str | None = None, text: str | None = None,
                   where: str = "", args: tuple = (), order: str = "id", limit: int = 200) -> list[dict]:
        """Items matching every condition given. `where` adds raw SQL over the columns and json_extract(data, ...)."""
        sql, params = ["1 = 1"], []
        for col, val in (("form", form), ("kind", kind), ("status", status)):
            if isinstance(val, (tuple, list)):
                sql.append(f"{col} IN ({','.join('?' * len(val))})"); params += list(val)
            elif val:
                sql.append(f"{col} = ?"); params.append(val)
        if open_only:
            closed = sorted({s for f in FORMS.values() for s in f.closed})
            sql.append(f"status NOT IN ({','.join('?' * len(closed))})"); params += closed
        if parent is not None:
            sql.append("parent_id = ?"); params.append(parent)
        if owner:
            sql.append("owner = ?"); params.append(owner)
        if text:
            for word in text.casefold().split():
                sql.append("lower(title || ' ' || data) LIKE ?"); params.append(f"%{word}%")
        if where:
            sql.append(where); params += list(args)
        rows = await self.db.fetchall(f"SELECT * FROM items WHERE {' AND '.join(sql)} ORDER BY {order} LIMIT ?", (*params, limit))
        return [self._flat(r) for r in rows]

    async def history(self, item_id: int) -> list[dict]:
        return [dict(r) for r in await self.db.fetchall("SELECT created_at, by, run_id, status, changes, note FROM item_log WHERE item_id = ? ORDER BY id", (item_id,))]

    async def links(self, item_id: int) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT from_id, to_id, relation FROM item_links WHERE from_id = ? OR to_id = ? ORDER BY created_at", (item_id, item_id))
        return [dict(r) for r in rows]

    # --- writing ------------------------------------------------------------------------------

    async def create(self, form: str, title: str, by: str, *, kind: str | None = None, status: str | None = None,
                     owner: str | None = None, parent: int | None = None, data: dict | None = None, source: dict | None = None,
                     review_at: str | None = None, valid_until: str | None = None, note: str | None = None,
                     item_id: int | None = None, check_rights: bool = False) -> int:
        if check_rights and not may(by, "create", form):
            raise FormError(f"{by} may not create a {form}.")
        form, kind, status, clean = check(form, kind, status, data or {}, title=title, source=source, review_at=review_at, creating=True)
        if parent is not None and not await self.get(parent):
            raise FormError(f"parent {parent} doesn't exist")
        item_id = await self.db.insert(
            "items", **({"id": item_id} if item_id else {}), form=form, kind=kind, title=title.strip(), status=status,
            owner=owner or (by if by in ("assistant", "director", "researcher", "manager") else None), parent_id=parent,
            source=json.dumps(source, ensure_ascii=False) if source else None, review_at=_day_or_time(review_at),
            valid_until=_day_or_time(valid_until), data=json.dumps({k: v for k, v in clean.items() if v is not None}, ensure_ascii=False), by=by,
        )
        await self._log(item_id, by, status, {"created": True}, note or "skapad")
        await trail.emit("item", by, ref=f"#{FORMS[form].letter}{item_id}", form=form, action="create")
        await self._changed(item_id)
        return item_id

    async def update(self, item_id: int, by: str, *, title: str | None = None, status: str | None = None, kind: str | None = None,
                     owner: str | None = None, parent: int | None | type(...) = ..., data: dict | None = None,
                     source: dict | None = None, review_at: str | None = None, valid_until: str | None = None,
                     note: str | None = None, check_rights: bool = False) -> list[str]:
        """Changes only what is given and differs; logs what changed. Returns the names of the changed fields."""
        item = await self.get(item_id)
        if item is None:
            raise FormError(f"no item {item_id}")
        if check_rights and not may(by, "update", item["form"], item["owner"]):
            raise FormError(f"{by} may not change {ref(item)} (owned by {item['owner']}).")
        new_kind = kind or item["kind"]
        _, new_kind, new_status, clean = check(item["form"], new_kind, status or item["status"], data or {}, title=title, source=source,
                                               review_at=review_at, creating=False)
        row = await self.db.fetchone("SELECT data FROM items WHERE id = ?", (item_id,))
        stored = json.loads(row["data"] or "{}")
        changes: dict = {}
        for k, v in clean.items():
            if stored.get(k) != v:
                changes[k] = v
                if v is None:
                    stored.pop(k, None)
                else:
                    stored[k] = v
        cols: dict = {}
        for col, val in (("title", title and title.strip()), ("status", new_status), ("kind", new_kind), ("owner", owner),
                         ("review_at", _day_or_time(review_at)), ("valid_until", _day_or_time(valid_until))):
            if val is not None and val != item[col]:
                cols[col] = changes[col] = val
        if parent is not ...:
            if parent is not None and (parent == item_id or not await self.get(parent)):
                raise FormError(f"parent {parent} doesn't exist or is the item itself")
            if parent != item["parent_id"]:
                cols["parent_id"] = changes["parent_id"] = parent
        if source is not None and source != item["source"]:
            cols["source"] = json.dumps(source, ensure_ascii=False)
            changes["source"] = source
        if not changes and not note:
            return []
        if changes:
            sets = ", ".join(f"{c} = ?" for c in cols) + (", " if cols else "") + "data = ?, updated_at = datetime('now')"
            await self.db.execute(f"UPDATE items SET {sets} WHERE id = ?", (*cols.values(), json.dumps(stored, ensure_ascii=False), item_id))
        await self._log(item_id, by, cols.get("status"), changes or None, note)
        await trail.emit("item", by, ref=ref(item), form=item["form"], action="update")
        if changes:
            await self._changed(item_id)
        return list(changes)

    async def link(self, from_id: int, to_id: int, relation: str, by: str) -> None:
        if not await self.get(from_id) or not await self.get(to_id):
            raise FormError("both items must exist")
        await self.db.execute(
            "INSERT OR IGNORE INTO item_links (from_id, to_id, relation, by) VALUES (?, ?, ?, ?)", (from_id, to_id, relation, by))

    async def _log(self, item_id: int, by: str, status: str | None, changes: dict | None, note: str | None) -> None:
        await self.db.insert("item_log", item_id=item_id, by=by, status=status,
                             changes=json.dumps(changes, ensure_ascii=False, default=str) if changes else None, note=note)

    # --- in words -----------------------------------------------------------------------------

    @staticmethod
    def line(item: dict) -> str:
        """One line: the id, what it is, its title and status."""
        form = FORMS[item["form"]]
        what = form.label + (f" ({item['kind']})" if item["kind"] else "")
        return f"{ref(item)} {what}: {item['title']} · {item['status']}"

    async def describe(self, item_id: int) -> str | None:
        """Everything about one item, for the roles: fields, source, what it serves and holds, and its history."""
        item = await self.get(item_id)
        if item is None:
            return None
        form = FORMS[item["form"]]
        lines = [self.line(item) + (f" · ägare {item['owner']}" if item["owner"] else "")]
        if item["parent_id"] and (p := await self.get(item["parent_id"])):
            lines.append(f"Under: {ref(p)} {p['title']}")
        for f in form.fields:
            if f.name in item:
                lines.append(f"{f.name}: {item[f.name]}")
        if item["source"]:
            lines.append("Källa: " + ", ".join(f"{k} {v}" for k, v in item["source"].items()))
        for k in ("review_at", "valid_until"):
            if item[k]:
                lines.append(f"{k}: {item[k]}")
        under = await self.find(parent=item_id, limit=50)
        if under:
            lines.append("Rymmer: " + ", ".join(self.line(u) for u in under))
        for link in await self.links(item_id):
            other = link["to_id"] if link["from_id"] == item_id else link["from_id"]
            if o := await self.get(other):
                lines.append(f"Länk ({link['relation']}): {self.line(o)}")
        hist = await self.history(item_id)
        if hist:
            lines.append("Historik:")
            lines += [f"- {h['created_at'][:10]} {h['by']}: {h['status'] or ''} {h['note'] or ''}".rstrip() for h in hist[-12:]]
        return "\n".join(lines)


def _day_or_time(value: str | None) -> str | None:
    if not value:
        return None
    v = str(value).strip()
    try:
        return date.fromisoformat(v[:10]).isoformat() + (v[10:] if len(v) > 10 else "")
    except ValueError as e:
        raise FormError(f"not a date: {value!r}") from e
