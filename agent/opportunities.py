"""Opportunities (#O, agent/forms.py): one item per real thing out there, a job posting, a programme, an event, so
that what the office says about it comes from one place with its exact link and the source of every fact.

Why (23 Oct): a search found the facts of Google's EMEA internship (Stockholm one of the places) but linked the London
posting, whose page it couldn't read. The caveat lay in a free-text field, the record of what the owner went for had
no link at all, and the owner was told the wrong deadline and the wrong place. Two postings with the same title for
different cities are two things; a copy of one posting on another site is the same thing.

How identity is decided:
- the search run sees the open opportunities it may meet, and says of each finding: the same as #O…, new, or unclear,
  and how it differs from similar ones (Researcher's judgement, where the understanding is);
- the code holds it to the addresses: a link that is already an opportunity's, or one of its other addresses, is
  that opportunity whatever the run said;
- unclear becomes a step for Researcher to sort out at once, so it is looked into rather than guessed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from agent.db import Database
from agent.forms import FormError
from agent.items import Items, parse_ref, ref

log = logging.getLogger(__name__)

KINDS = {"opportunity": "job", "event": "event", "job": "job"}   # a finding's kind -> the opportunity's kind, by default
OPEN = ("open", "upcoming")


def _norm(url: str | None) -> str:
    from agent.research import normalize_url

    return normalize_url(url) if url else ""


def addresses(item: dict) -> set[str]:
    return {_norm(item.get("url"))} | {_norm(u) for u in (item.get("also_at") or "").splitlines() if u.strip()}


async def by_address(db: Database, url: str | None) -> dict | None:
    """The opportunity this exact address belongs to, if any (its own link or one of its other addresses)."""
    key = _norm(url)
    if not key:
        return None
    for o in await Items(db).find("opportunity", order="id DESC", limit=1000):
        if key in addresses(o):
            return o
    return None


async def known(db: Database, limit: int = 40) -> str:
    """The open opportunities, for a search run to recognise what it finds."""
    rows = await Items(db).find("opportunity", status=OPEN, order="id DESC", limit=limit)
    lines = []
    for o in rows:
        bits = [o.get("organization") or "", o.get("place") or "", f"sista dag {o['deadline']}" if o.get("deadline") else "",
                f"datum {o['date']}" if o.get("date") else ""]
        lines.append(f"- {ref(o)} {o['kind']}: {o['title']} · " + " · ".join(b for b in bits if b) + f" · {o['url']}"
                     + (f" (också: {', '.join(sorted(a for a in addresses(o) if a != _norm(o['url'])))})" if o.get("also_at") else "")
                     + (f" · skiljer sig från: {o['differs_from']}" if o.get("differs_from") else ""))
    return "\n".join(lines) or "(none yet)"


def _fields(f: dict) -> dict:
    return {k: v for k, v in {
        "url": f.get("url"), "organization": f.get("organization") or "okänd", "place": f.get("location"),
        "deadline": f.get("deadline"), "date": f.get("date"), "who": f.get("who"), "checked": f.get("checked"),
        "unconfirmed": f.get("unconfirmed"), "differs_from": f.get("identity_note") if f.get("identity") != "same" else None,
    }.items() if v not in (None, "")}


async def record(db: Database, finding: dict, by: str = "researcher", mission: str | None = None) -> tuple[int | None, str]:
    """Links a finding to its opportunity, or makes a new one. Returns (#O id, how): 'address' (the link was known),
    'same' (the run recognised it; the link is added as another address), 'unclear' (made apart, with a step to sort
    it out), 'new'. Only things one can apply to or go to are opportunities."""
    kind = KINDS.get(finding.get("kind") or "")
    if kind is None or not finding.get("url"):
        return None, "not an opportunity"
    items = Items(db)
    found = await by_address(db, finding["url"])
    if found is not None:
        await _refresh(items, found, finding, by)
        return found["id"], "address"
    same = parse_ref(str(finding.get("same_as") or "")) if finding.get("identity") == "same" else None
    target = await items.get(same) if same else None
    if target is not None and target["form"] == "opportunity":
        also = "\n".join(sorted((addresses(target) - {_norm(target["url"])}) | {_norm(finding["url"])}))
        await items.update(target["id"], by, data={"also_at": also}, note=f"samma sak på en annan adress: {finding['url']}")
        await _refresh(items, await items.get(target["id"]), finding, by)
        return target["id"], "same"
    try:
        oid = await items.create("opportunity", finding["title"][:200], by, kind=kind, owner="researcher",
                                 status="open", data=_fields(finding), note=f"hittad av sökuppdraget {mission}" if mission else "hittad")
    except FormError as e:
        log.warning("couldn't record an opportunity for %s: %s", finding.get("url"), e)
        return None, "invalid"
    if finding.get("identity") == "unclear":
        other = parse_ref(str(finding.get("same_as") or ""))
        about = f" och {ref(await items.get(other))}" if other and await items.get(other) else ""
        step = await items.create(
            "step", f"Reda ut om #O{oid}{about} är samma sak", "system", owner="researcher", parent=oid,
            review_at=f"{datetime.now() + timedelta(days=1):%Y-%m-%d}",
            data={"action": "research", "done_when": "Avgjort: samma (slå ihop: den ena 'gone' med hänvisning, adressen till den andra) eller olika (skriv hur i differs_from)"},
            note=finding.get("identity_note") or "oklart om det är samma som något vi redan har")
        log.info("opportunity #O%s: unclear identity, step #T%s to sort it out", oid, step)
        return oid, "unclear"
    return oid, "new"


async def _refresh(items: Items, o: dict, finding: dict, by: str) -> None:
    """A finding checked on the page itself may correct what an earlier, weaker one said; never the other way."""
    if finding.get("checked") != "page" or _norm(finding.get("url")) != _norm(o["url"]):
        return
    new = {k: v for k, v in _fields(finding).items() if k in ("place", "deadline", "date", "who") and v and v != o.get(k)}
    if o.get("checked") != "page" or new:
        await items.update(o["id"], by, data={**new, "checked": "page", "unconfirmed": None},
                           note="bekräftat på sidan" + (f": {', '.join(new)}" if new else ""))


async def follow(db: Database, item: dict) -> None:
    """Items watcher: what the owner goes for keeps the link and dates of the opportunity it is about."""
    if item["form"] != "opportunity":
        return
    items = Items(db)
    for aim in await items.find("aim", where="json_extract(data, '$.opportunity') = ?", args=(item["id"],)):
        change = {k: item.get(src) for k, src in (("url", "url"), ("deadline", "deadline")) if item.get(src) and item.get(src) != aim.get(k)}
        if change:
            await items.update(aim["id"], "system", data=change, note=f"från {ref(item)}")
