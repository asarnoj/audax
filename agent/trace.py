"""Follow one thing the office did back to why (./deploy/audax trace <id>).

Audax works mostly out of sight, so whether it works is judged by its concrete acts: what the run that did it was
shown, what it did with it, and where each piece it leaned on came from. Every row a run writes carries the run's id
(agent/trail.py), every memory commit names its run, and every run keeps its prompt and its tool calls, so the chain
can be rebuilt rather than guessed. A fault found on the way is then named by its cause: how the prompt was built,
what the architecture keeps (or doesn't), or an earlier run's mistake carried forward.

Targets: R123 (a run), M123 (a message), F123 (a finding), P123 (a post), B123 (a booking), D123 (a point to
decide), an item (S5, U31, T12 …), or a line of a text: policy.md:<a few words of it>. Add 'prompt' after a run to
print its whole prompt and transcript.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent.db import Database
from agent.forms import FORMS, LETTERS
from agent.items import Items, mentions, ref

ID = re.compile(r"#?([" + "".join(LETTERS) + r"])(\d+)\b")
LINES = 12        # prompt lines shown around what is traced
WIDTH = 230


class Trace:
    def __init__(self, db: Database, memory: Path | None = None, tz: str = "Europe/Stockholm"):
        self.db = db
        self.memory = memory
        self.tz = ZoneInfo(tz)
        self.items = Items(db)

    # --- small helpers ----------------------------------------------------------------------

    def when(self, utc: str | None) -> str:
        if not utc:
            return "?"
        return datetime.fromisoformat(utc[:19] + "+00:00").astimezone(self.tz).strftime("%a %d/%m %H:%M")

    async def run_line(self, run_id: int | None) -> str:
        if not run_id:
            return "koden (ingen Claude-körning)"
        r = await self.db.fetchone("SELECT id, kind, created_at FROM claude_runs WHERE id = ?", (run_id,))
        return f"#R{r['id']} {r['kind']} {self.when(r['created_at'])}" if r else f"#R{run_id}"

    async def origin(self, item_id: int) -> str:
        """Where an item came from and who last changed it, in one line."""
        item = await self.items.get(item_id)
        if item is None:
            return f"{item_id}: finns inte"
        first = await self.db.fetchone("SELECT created_at, by, run_id FROM item_log WHERE item_id = ? ORDER BY id LIMIT 1", (item_id,))
        last = await self.db.fetchone("SELECT created_at, by, run_id, note FROM item_log WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item_id,))
        src = (item.get("source") or {}).get("type")
        line = f"{Items.line(item)}"
        if src:
            line += f" · källa {src}"
        if first:
            line += f"\n    skapad {self.when(first['created_at'])} av {first['by']} i {await self.run_line(first['run_id'])}"
        if last and first and last != first:
            line += f"; senast ändrad {self.when(last['created_at'])} av {last['by']} i {await self.run_line(last['run_id'])}" \
                    + (f": {last['note'][:100]}" if last["note"] else "")
        return line

    # --- a run --------------------------------------------------------------------------------

    async def run(self, run_id: int, focus: set[str] | None = None, whole: bool = False) -> str:
        r = await self.db.fetchone("SELECT * FROM claude_runs WHERE id = ?", (run_id,))
        if r is None:
            return f"Ingen körning #R{run_id}."
        prompt = r["prompt"] or ""
        steps = json.loads(r["transcript"] or "[]")
        if whole:
            return prompt + "\n\n===== TRANSCRIPT =====\n" + "\n".join(json.dumps(s, ensure_ascii=False) for s in steps)
        out = [f"Körning #R{run_id}: {r['kind']}, {self.when(r['created_at'])}"
               + (f", {r['turns']} turer" if r["turns"] else "") + (f", {r['cost_usd']:.2f} $" if r["cost_usd"] else "")
               + (", MISSLYCKADES: " + (r["error"] or "")[:200] if r["is_error"] else "")]
        if prompt:
            out.append(f"Prompten: {len(prompt)} tecken. Största delarna: " + ", ".join(
                f"{h} ({n})" for n, h in sections(prompt)[:6]))
            if focus:
                hits = [ln.strip() for ln in prompt.splitlines() if any(f in ln for f in focus) and ln.strip()]
                if hits:
                    out.append(f"I prompten om {', '.join(sorted(focus))}:\n" + "\n".join(f"  | {h[:WIDTH]}" for h in hits[:LINES])
                               + (f"\n  | …({len(hits) - LINES} rader till)" if len(hits) > LINES else ""))
                else:
                    out.append(f"Prompten nämner inte {', '.join(sorted(focus))}.")
        else:
            out.append("Prompten sparades inte (äldre än 60 dagar, eller en körning som inte sparar sin prompt).")
        calls = [s for s in steps if "tool" in s]
        if calls:
            lines = []
            for i, s in enumerate(steps):
                if "tool" in s:
                    res = steps[i + 1] if i + 1 < len(steps) and "result" in steps[i + 1] else {}
                    tail = " → FEL: " + res.get("result", "")[:120] if res.get("error") else ""
                    lines.append(f"  {s['tool'].split('__')[-1]}: {_flat(s.get('input'))[:180]}{tail}")
            out.append(f"Vad den gjorde ({len(calls)} anrop):\n" + "\n".join(lines[:25]))
        if r["output"]:
            out.append("Svar: " + _flat(r["output"])[:600])
        return "\n".join(out)

    # --- the things it can follow --------------------------------------------------------------

    async def item(self, item_id: int) -> str:
        item = await self.items.get(item_id)
        if item is None:
            return f"Ingen post {item_id}."
        out = [f"Post {Items.line(item)}" + (f" · ägare {item['owner']}" if item["owner"] else "")]
        form = FORMS[item["form"]]
        for f in form.fields:
            if item.get(f.name) not in (None, ""):
                out.append(f"  {f.name}: {str(item[f.name])[:300]}")
        hist = await self.db.fetchall("SELECT * FROM item_log WHERE item_id = ? ORDER BY id", (item_id,))
        out.append("Historik (vem, i vilken körning, vad):")
        for h in hist[-15:]:
            changed = ", ".join(k for k in json.loads(h["changes"] or "{}") if k not in ("was", "created")) if h["changes"] else ""
            out.append(f"  {self.when(h['created_at'])} {h['by']} i {await self.run_line(h['run_id'])}"
                       + (f" · status {h['status']}" if h["status"] else "") + (f" · ändrade {changed}" if changed else "")
                       + (f" · {h['note'][:140]}" if h["note"] else ""))
        named = sorted({n for _, n in mentions(*(str(item.get(f.name) or "") for f in form.fields), item["title"])} - {item_id})
        if named:
            out.append("Det den nämner, och varifrån det kom:")
            out += [f"  {await self.origin(n)}" for n in named[:10]]
        if item["form"] == "belief":
            uses = await self.db.fetchall("SELECT module, decision, count(*) AS n FROM belief_uses WHERE belief_id = ? GROUP BY module, decision",
                                          (item_id,))
            if uses:
                out.append("Beslut som lutade sig mot den: " + ", ".join(f"{u['module']} {u['decision']} ×{u['n']}" for u in uses))
        last_run = next((h["run_id"] for h in reversed(hist) if h["run_id"]), None)
        if last_run:
            out.append("\n" + await self.run(last_run, {ref(item)}))
        return "\n".join(out)

    async def finding(self, cid: int) -> str:
        c = await self.db.fetchone("SELECT * FROM candidates WHERE id = ?", (cid,))
        if c is None:
            return f"Inget fynd #F{cid}."
        feats = json.loads(c["features"] or "{}")
        out = [f"Fynd #F{cid} ({c['kind']}): {c['title']} · {c['status']}" + (f" · {c['url']}" if c["url"] else ""),
               f"  hittat {self.when(c['created_at'])}" + (f" · måste fram senast {c['latest_useful']}" if c["latest_useful"] else "")]
        if c["event_id"] and (e := await self.db.fetchone("SELECT * FROM events WHERE id = ?", (c["event_id"],))):
            out.append(f"  kom från {e['source']} (händelse {e['id']}, {self.when(e['created_at'])})")
        if feats:
            out.append("  triagens bedömning: " + ", ".join(f"{k} {v}" for k, v in feats.items() if k not in ("reason", "brief"))[:300])
            if feats.get("reason"):
                out.append(f"  skäl: {feats['reason'][:300]}")
        uses = await self.db.fetchall("SELECT belief_id, module, decision FROM belief_uses WHERE candidate_id = ?", (cid,))
        if uses:
            out.append("Lutade sig mot:")
            for u in uses:
                out.append(f"  {u['module']} {u['decision']}: {await self.origin(u['belief_id'])}")
        decs = await self.db.fetchall("SELECT * FROM decisions WHERE candidate_id = ? ORDER BY id", (cid,))
        if decs:
            out.append("Beslut i ordning:")
            for d in decs:
                out.append(f"  {self.when(d['created_at'])} {d['module']}: {d['tier']} i {await self.run_line(d['run_id'])} · {(d['reason'] or '')[:220]}")
        msgs = await self.db.fetchall("SELECT id, created_at, text, run_id FROM messages WHERE candidate_id = ? ORDER BY id", (cid,))
        for m in msgs:
            fb = await self.db.fetchall("SELECT signal, value FROM feedback WHERE message_id = ?", (m["id"],))
            out.append(f"Skickat M{m['id']} {self.when(m['created_at'])} (skrivet i {await self.run_line(m['run_id'])}): \"{_flat(m['text'])[:200]}\""
                       + (f" · reaktion: {', '.join(f['signal'] + ' ' + (f['value'] or '') for f in fb)}" if fb else ""))
        for d in decs:   # the judging runs, each seen from this finding
            if d["run_id"] and d["module"].startswith(("triage", "dispatcher", "rejudge")):
                out.append("\n" + await self.run(d["run_id"], {f"#F{cid}", f"candidate_id\": {cid}", f"[event_id {c['event_id']}]",
                                                               f"candidate_id {cid}"}))
        return "\n".join(out)

    async def message(self, mid: int) -> str:
        m = await self.db.fetchone("SELECT * FROM messages WHERE id = ?", (mid,))
        if m is None:
            return f"Inget meddelande M{mid}."
        who = "ägaren" if m["direction"] == "in" else "Audax"
        refs = [r["ref"] for r in await self.db.fetchall("SELECT ref FROM message_refs WHERE message_id = ?", (mid,))]
        out = [f"Meddelande M{mid} från {who}, {self.when(m['created_at'])} ({m['origin'] or 'samtal'}): \"{_flat(m['text'])[:500]}\""]
        if refs:
            out.append("  handlar om: " + ", ".join(refs))
        fb = await self.db.fetchall("SELECT signal, value FROM feedback WHERE message_id = ?", (mid,))
        if fb:
            out.append("  reaktion: " + ", ".join(f"{f['signal']} {f['value'] or ''}" for f in fb))
        if m["run_id"]:
            out.append("\n" + await self.run(m["run_id"], set(refs) | {_flat(m["text"])[:40]}))
        if m["candidate_id"]:
            out.append("\n" + await self.finding(m["candidate_id"]))
        return "\n".join(out)

    async def post(self, pid: int) -> str:
        p = await self.db.fetchone("SELECT * FROM posts WHERE id = ?", (pid,))
        if p is None:
            return f"Ingen post #P{pid}."
        out = [f"Post #P{pid} ({p['kind']}) från {p['sender']} till {p['recipient']}, {self.when(p['created_at'])} · {p['status']}",
               f"  \"{_flat(p['body'])[:500]}\""]
        if p["verbatim"]:
            out.append(f"  ägarens ord: \"{_flat(p['verbatim'])[:300]}\"")
        if p["outcome"]:
            out.append(f"  utfall: {p['outcome'][:300]}")
        handled = await self.db.fetchall("SELECT id FROM claude_runs WHERE prompt LIKE ? AND created_at >= ? ORDER BY id LIMIT 3",
                                         (f"%#P{pid}%", p["created_at"]))
        if p["run_id"]:
            out.append("\nSkriven i:\n" + await self.run(p["run_id"], {f"#P{pid}"}))
        for h in handled:
            out.append("\nLästes i:\n" + await self.run(h["id"], {f"#P{pid}"}))
        return "\n".join(out)

    async def booking(self, bid: int) -> str:
        b = await self.db.fetchone("SELECT * FROM bookings WHERE id = ?", (bid,))
        if b is None:
            return f"Ingen bokning #B{bid}."
        out = [f"Bokning #B{bid} ({b['kind']}): {b['title']} · {b['status']} · {b['due_at']}"
               + (f" · om {b['ref']}" if b["ref"] else ""), f"  {_flat(b['body'])[:300]}"]
        if b["run_id"]:
            out.append("\n" + await self.run(b["run_id"], {f"#B{bid}"}))
        return "\n".join(out)

    async def point(self, pid: int) -> str:
        from agent import docket

        p = await docket.get(self.db, pid)
        if p is None:
            return f"Ingen punkt D{pid}."
        out = [f"Punkt D{pid} hos {p['role']} [{p['kind']}]: {p['title']} · {p['status']}, visad {p['shown']} gång(er)",
               "  " + p["basis"].replace("\n", "\n  ")[:2500]]
        if p["answer"]:
            out.append(f"  svar ({p['decision'] or 'senare'}): {p['answer']}")
        runs = await self.db.fetchall("SELECT id FROM claude_runs WHERE created_at >= ? AND transcript LIKE ? ORDER BY id LIMIT 2",
                                      (p["created_at"], f"%D{pid}%"))
        for r in runs:
            out.append("\n" + await self.run(r["id"], {f"D{pid}"}))
        if not runs:
            out.append("Ingen körning har svarat på den än.")
        return "\n".join(out)

    async def line(self, name: str, words: str) -> str:
        """A line of a role's text: who wrote it, in which run (the memory's commits name their run)."""
        if self.memory is None:
            return "Minnets sökväg saknas."
        path = self.memory / name
        text = path.read_text() if path.exists() else ""
        found = [ln for ln in text.splitlines() if words.lower() in ln.lower()]
        out = [f"{name}: " + (f"\"{found[0].strip()[:300]}\"" if found else f"ingen rad med \"{words}\" nu")]
        log = subprocess.run(["git", "-C", str(self.memory), "log", "-S", words, "--format=%h %ad %s", "--date=format:%d/%m %H:%M",
                              "--", name], capture_output=True, text=True).stdout.strip().splitlines()
        if log:
            out.append("Ändringar som lade till eller tog bort de orden (nyast först):")
            out += [f"  {x}" for x in log[:8]]
            first = re.search(r"#R(\d+)", log[-1])
            if first:
                out.append("\nKörningen som först skrev det:\n" + await self.run(int(first.group(1)), {words}))
        for letter, num in ID.findall(found[0] if found else ""):
            out.append(f"Raden hänvisar till: {await self.origin(int(num))}")
        return "\n".join(out)

    async def __call__(self, target: str, extra: str = "") -> str:
        t = target.strip().lstrip("#")
        if ":" in t and t.split(":", 1)[0].endswith(".md"):
            name, words = t.split(":", 1)
            return await self.line(name, words.strip())
        head, num = t[:1].upper(), t[1:]
        if not num.isdigit():
            return f"Förstår inte {target!r}: R123, M123, F123, P123, B123, D123, ett id som S5 eller U31, eller fil.md:ord"
        n = int(num)
        if head == "R":
            return await self.run(n, whole=extra == "prompt")
        if head == "M":
            return await self.message(n)
        if head == "F":
            return await self.finding(n)
        if head == "P":
            return await self.post(n)
        if head == "B":
            return await self.booking(n)
        if head == "D":
            return await self.point(n)
        if head in LETTERS:
            return await self.item(n)
        return f"Okänd sort {head!r}."


def sections(prompt: str) -> list[tuple[int, str]]:
    """The prompt's parts by size: its headings, and the files it reads ('--- policy.md ---')."""
    marks = [(m.start(), m.group(0).strip("#- ").strip()[:50]) for m in re.finditer(r"(?m)^(#{1,2} .+|--- .+ ---)$", prompt)]
    out = []
    for i, (start, head) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(prompt)
        out.append((end - start, head))
    return sorted(out, reverse=True)


def _flat(value) -> str:
    return " ".join(str(value or "").split())


async def _main(argv: list[str]) -> None:
    args = [a for a in argv if not a.startswith("--")]
    opts = dict(a[2:].split("=", 1) for a in argv if a.startswith("--") and "=" in a)
    if not args:
        print(__doc__)
        return
    db = Database(Path(opts.get("db", "data/agent.db")))
    await db.open()
    try:
        memory = Path(opts.get("memory", "workspace/memory"))
        print(await Trace(db, memory if memory.exists() else None, opts.get("tz", "Europe/Stockholm"))(args[0], args[1] if len(args) > 1 else ""))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1:]))
