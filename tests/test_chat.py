import asyncio

import pytest

from agent.chat import SESSION_KEY, ChatSession, Incoming
from agent.claude_gate import ClaudeGate, RunResult
from agent.config import load_settings
from agent.db import Database
from agent import tools as tools_mod
from agent.tools import TurnContext, build_chat_tools


class FakeMemory:
    async def commit_if_changed(self, message, by=None):
        return []

    def read(self, *names):
        return ""


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


async def wait_idle(chat: ChatSession):
    for _ in range(100):
        await asyncio.sleep(0.01)
        if chat._worker is None or chat._worker.done():
            return


async def test_messages_during_busy_turn_are_batched(db, tmp_path):
    prompts, resumes = [], []
    release = asyncio.Event()

    async def runner(task, ws, on_rl):
        prompts.append(task.prompt)
        resumes.append(task.resume)
        if len(prompts) == 1:
            await release.wait()
        return RunResult(text=f"svar {len(prompts)}", session_id="s1")

    sent = []

    async def send(text, candidate_id=None, origin=None, in_session=False):
        sent.append(text)
        return []

    async def typing():
        pass

    chat = ChatSession(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), FakeMemory(), send, typing)
    chat.submit(Incoming(text="första"))
    await asyncio.sleep(0.01)
    chat.submit(Incoming(text="andra"))
    chat.submit(Incoming(text="tredje", replied_to_text="ett tidigare meddelande"))
    release.set()
    await wait_idle(chat)

    assert len(prompts) == 2
    assert "första" in prompts[0]
    assert "andra" in prompts[1] and "tredje" in prompts[1] and '[replying to: "ett tidigare meddelande"]' in prompts[1]
    assert resumes == [None, "s1"]  # second turn resumes the stored session
    assert sent == ["svar 1", "svar 2"]
    assert await db.kv_get(SESSION_KEY) == "s1"


async def test_failed_resume_falls_back_to_new_session(db, tmp_path):
    await db.kv_set(SESSION_KEY, "gone")
    await db.execute("INSERT INTO messages (telegram_message_id, direction, text, origin) VALUES (1, 'out', 'Tidigare tips om Google', 'proactive')")
    calls, prompts = [], []

    async def runner(task, ws, on_rl):
        calls.append(task.resume)
        prompts.append(task.prompt)
        if task.resume == "gone":
            return RunResult(text="", is_error=True, error="No conversation found")
        return RunResult(text="hej", session_id="fresh")

    sent = []

    async def send(text, candidate_id=None, origin=None, in_session=False):
        sent.append(text)
        return []

    async def typing():
        pass

    chat = ChatSession(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), FakeMemory(), send, typing)
    chat.submit(Incoming(text="hallå"))
    await wait_idle(chat)
    assert calls == ["gone", None]
    assert "Tidigare tips om Google" in prompts[1]  # the new session gets the recent conversation
    assert sent == ["hej"]
    assert await db.kv_get(SESSION_KEY) == "fresh"


async def test_record_feedback_links_to_replied_message(db, monkeypatch):
    msg_id = await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (10, 'out', 'tips')")

    captured = {}
    real = tools_mod.create_sdk_mcp_server

    def capture(name, version="1.0.0", tools=None):
        captured["tools"] = tools
        return real(name, version=version, tools=tools)

    monkeypatch.setattr(tools_mod, "create_sdk_mcp_server", capture)
    _, names = build_chat_tools(db, TurnContext(replied_to_message_id=msg_id))
    assert names == ["mcp__agent__record_feedback", "mcp__agent__interview_guide", "mcp__agent__report_problem", "mcp__agent__track_pursuit", "mcp__agent__lookup", "mcp__agent__remember"]

    await captured["tools"][0].handler({"target": "replied", "sentiment": "negative", "aspect": "amount", "note": "för långt"})
    row = await db.fetchone("SELECT message_id, signal, value, note FROM feedback")
    assert tuple(row) == (msg_id, "text", "negative:amount", "för långt")


async def test_proactive_messages_are_shown_to_the_chat_once(db, tmp_path):
    prompts = []

    async def runner(task, ws, on_rl):
        prompts.append(task.prompt)
        return RunResult(text="SVAR-123", session_id="s")

    async def send(text, candidate_id=None, origin=None, in_session=False):
        return [await db.execute("INSERT INTO messages (telegram_message_id, direction, text, origin, in_session) VALUES (0, 'out', ?, ?, ?)", (text, origin, int(in_session)))]

    async def typing():
        pass

    await db.execute("INSERT INTO messages (telegram_message_id, direction, text, origin) VALUES (5, 'out', 'Google-praktik i Stockholm', 'proactive')")
    chat = ChatSession(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), FakeMemory(), send, typing)
    chat.submit(Incoming(text="kolla Google närmare"))
    await wait_idle(chat)
    chat.submit(Incoming(text="tack"))
    await wait_idle(chat)

    assert "[you sent on your own" in prompts[0] and "Google-praktik i Stockholm" in prompts[0]
    assert "Google-praktik" not in prompts[1]  # shown once
    assert "SVAR-123" not in prompts[1]  # the chat's own replies are never echoed back



async def test_a_delivery_is_written_with_a_short_brief_and_the_conversation_hears_of_it_once(db, tmp_path):
    from agent.pipeline.outbox import AlreadySaid

    tasks = []

    async def runner(task, ws, on_rl):
        tasks.append(task)
        if task.kind == "deliver":
            reply = "" if "redan sagt" in task.prompt else "Hackathon på Uni i nov, passar sidoprojektet."
            return RunResult(text="", structured={"message": reply})
        return RunResult(text="", structured={"message": "svar"}, session_id="s")

    async def send(text, candidate_id=None, origin=None, in_session=False):
        return [await db.execute("INSERT INTO messages (telegram_message_id, direction, text, origin, in_session) VALUES (0, 'out', ?, ?, ?)", (text, origin, int(in_session)))]

    async def typing():
        pass

    chat = ChatSession(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), FakeMemory(), send, typing)
    chat.submit(Incoming(text="hej"))  # The owner writes while the dispatcher wants to speak
    ids = await chat.speak("Berätta kort om: Uni-hackathon i november", about={"#F7"})
    await wait_idle(chat)

    assert [t.kind for t in tasks] == ["chat", "deliver"]   # in order: the delivery sees the reply that came before it
    deliver = tasks[1]
    assert deliver.resume is None and "svar" in deliver.prompt and "Uni-hackathon" in deliver.prompt   # its own short brief
    row = await db.fetchone("SELECT origin, in_session, text FROM messages WHERE id = ?", (ids[0],))
    assert (row["origin"], row["in_session"]) == ("proactive", 0)
    assert await db.fetchone("SELECT 1 FROM message_refs WHERE message_id = ? AND ref = '#F7'", (ids[0],))
    chat.submit(Incoming(text="kul"))
    await wait_idle(chat)
    assert "Hackathon på Uni" in tasks[-1].prompt and "about #F7" in tasks[-1].prompt   # the conversation hears of it, once
    chat.submit(Incoming(text="ok"))
    await wait_idle(chat)
    assert "Hackathon på Uni" not in tasks[-1].prompt
    with pytest.raises(AlreadySaid):
        await chat.speak("redan sagt: samma hackathon")


async def test_speak_failure_raises_so_caller_can_fall_back(db, tmp_path):
    async def runner(task, ws, on_rl):
        return RunResult(text="", is_error=True, error="boom")

    async def send(text, **kw):
        raise AssertionError("must not send anything on internal failure")

    async def typing():
        pass

    chat = ChatSession(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), FakeMemory(), send, typing)
    with pytest.raises(RuntimeError):
        await chat.speak("säg något")


async def test_open_question_is_offered_and_the_answer_goes_back_to_whoever_asked(db, tmp_path):
    from agent.posts import Posts
    from agent.tools import TurnContext, _post_tools

    posts = Posts(db)
    qid = await posts.send("director", "assistant", "question", "Varför står sidoprojektet still?", context="Tid eller motivation?")
    prompts = []

    async def runner(task, ws, on_rl):
        prompts.append(task.prompt)
        return RunResult(text="ok", session_id="s")

    async def send(text, **kw):
        return []

    async def typing():
        pass

    chat = ChatSession(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), FakeMemory(), send, typing)
    chat.submit(Incoming(text="tråkig dag"))
    await wait_idle(chat)
    assert f"[open question #P{qid} from director" in prompts[0]

    await posts.asked(qid)
    assert await posts.open_question() is None  # one question at a time
    turn = TurnContext()
    tools = {t.name: t for t in _post_tools(posts, turn)}
    await tools["question"].handler({"id": f"#P{qid}", "answer": "Jag har fastnat på hur jag ska nå användare"})
    [answer] = await posts.inbox("director")  # back to the one who asked
    assert answer["verbatim"].startswith("Jag har fastnat") and answer["question"].startswith("Varför") and answer["kind"] == "answer"
    assert (await posts.get(qid))["status"] == "done" and f"#P{answer['id']}" in turn.refs

    from agent.items import Items

    q = await Items(db).create("question", "Räknas forskningspraktik?", "researcher", kind="clarify", review_at="2030-01-01", data={"to": "owner", "why": "Avgränsning"})
    await tools["question"].handler({"id": f"#Q{q}", "asked": True})
    assert (await Items(db).get(q))["status"] == "asked"
    await tools["question"].handler({"id": f"#Q{q}", "answer": "Ja, om det är betalt"})
    item = await Items(db).get(q)
    assert item["status"] == "answered" and item["answer"] == "Ja, om det är betalt"


async def test_only_the_owners_words_may_wake_a_role(db):
    from agent.posts import Posts

    woken = []
    Posts.waker = woken.append
    try:
        await Posts(db).send("assistant", "director", "note", "hitta praktik nu", verbatim="hjälp mig hitta praktik", from_owner=True, wake=True)
        await Posts(db).send("manager", "director", "note", "Julius är stressad", wake=True)  # role to role: waits
    finally:
        Posts.waker = None
    assert woken == ["director"]
    assert len(await Posts(db).inbox("director")) == 2


async def test_change_watch_stops_a_source_and_tells_the_director(db, monkeypatch):
    from agent import direction_ops as ops
    from agent.config import instance_config
    from agent.posts import Posts
    from agent.tools import _post_tools

    from agent import routines

    await routines.put_stream(db, {"id": "hn-best", "type": "rss", "url": "https://hn"}, "system")
    await routines.refresh(db)
    posts = Posts(db)
    tools = {t.name: t for t in _post_tools(posts)}

    out = await tools["change_watch"].handler({"source": "hn", "action": "stop", "why": "behöver inte HN"})
    assert "avstängd" in out["content"][0]["text"]
    assert (await routines.find(db, "stream", "hn-best"))["status"] == "paused"
    note = (await posts.inbox("researcher"))[0]  # watched sources are Researcher's
    assert note["verbatim"] == "behöver inte HN" and "hn-best" in note["body"]

    out = await tools["change_watch"].handler({"source": "nope", "action": "stop"})
    assert "Ingen bevakning matchar" in out["content"][0]["text"]


async def test_lookup_finds_ids_and_words_and_messages_link_to_them(db):
    from agent import refs
    from agent.pursuits import Pursuits

    sid, _ = await Pursuits(db).upsert({"title": "Google SRE Intern", "deadline": "2026-10-23"}, "chat")
    mid = await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (1, 'out', 'Jag påminner dig om Google den 16:e')")
    await refs.link(db, [mid], {f"#S{sid}"})
    assert "Google SRE Intern" in await refs.lookup(db, f"#S{sid}")
    found = await refs.lookup(db, "google")
    assert f"#S{sid}" in found and "påminner dig om Google" in found and f"[#S{sid}]" in found


def test_the_assistant_can_only_write_its_own_files():
    from agent.chat import CHAT_TOOLS

    writes = [t for t in CHAT_TOOLS if t.startswith(("Edit", "Write"))]
    assert writes == ["Edit(./memory/arbetsminne/assistant.md)", "Write(./memory/arbetsminne/assistant.md)"]  # the profile is a view of the beliefs


def test_memory_is_new_until_the_profile_differs_from_the_template(tmp_path):
    from agent.config import ROOT
    from agent.memory import MemoryRepo

    mem = MemoryRepo(tmp_path)
    assert mem.is_new()  # no profile at all
    (tmp_path / "profile.md").write_text((ROOT / "templates" / "memory" / "profile.md").read_text())
    assert mem.is_new()  # still the template: the interview starts on /start
    (tmp_path / "profile.md").write_text("# Profil\n\n## Vem\n- Studerar på KTH\n")
    assert not mem.is_new()


def test_only_the_reply_field_reaches_the_owner():
    from agent.chat import message_of

    narrated = RunResult(text="No URL there. Searching the conversation.\n\nHär är länken: https://g",
                         final_text="Här är länken: https://g", trace=[{"tool": "agent__lookup"}],
                         structured={"message": "Här är länken: https://g"})
    assert message_of(narrated) == "Här är länken: https://g"   # 17 and 19 Oct: the narration went out too
    no_field = RunResult(text="Tänker högt.\n\nSvaret.", final_text="Svaret.", trace=[{"tool": "agent__lookup"}])
    assert message_of(no_field) == "Svaret."   # never the text between tool calls
    plain = RunResult(text="hej")
    assert message_of(plain) == "hej"
    assert message_of(RunResult(text="", structured={"message": ""})) == ""   # an internal task with nothing new


async def test_search_finds_the_owners_own_words_and_says_who_and_when(db):
    from agent import refs

    await db.execute("INSERT INTO messages (telegram_message_id, direction, text, created_at) VALUES "
                     "(1, 'in', 'Tyvärr måste jag bestämma track 15e oktober och utbyte väldigt snart', '2026-10-12 18:17:07')")
    await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (2, 'out', 'Google: https://www.google.com/about/careers/x-intern-2027')")
    found = await refs.lookup(db, "trackval 15 oktober deadline")   # 19 Oct: this found nothing, and the chat said it wasn't the owner's
    assert "2026-10-12 20:17 Julius: Tyvärr måste jag bestämma track 15e oktober" in found
    assert "careers/x-intern-2027" in await refs.lookup(db, "google careers länk")


async def test_what_the_owner_says_becomes_a_belief_and_the_profile_follows(db, tmp_path):
    from agent.beliefs import current, write_profile
    from agent.items import Items
    from agent.tools import build_chat_tools

    import agent.tools as tools_mod

    captured = {}
    real = tools_mod.create_sdk_mcp_server
    tools_mod.create_sdk_mcp_server = lambda name, version="1.0.0", tools=None: captured.setdefault("tools", tools) and real(name, version=version, tools=tools)
    try:
        msg = await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (1, 'in', 'Jag vill inte jobba på banker')")
        turn = TurnContext(message_row_id=msg)
        build_chat_tools(db, turn)
    finally:
        tools_mod.create_sdk_mcp_server = real
    remember = {t.name: t for t in captured["tools"]}["remember"]
    out = await remember.handler({"statement": "Vill inte jobba på bank", "kind": "preference", "area": "not_interested",
                                  "subject": "bank", "quote": "Jag vill inte jobba på banker", "bubble": "excludes"})
    old = int(out["content"][0]["text"].split("#U")[1].split(".")[0].split(",")[0])
    assert (await Items(db).get(old))["source"]["message"] == msg
    out = await remember.handler({"statement": "Tekniska roller på bank går bra, inte rådgivning eller sälj", "kind": "preference",
                                  "area": "not_interested", "subject": "bank", "quote": "en teknisk roll på en bank kan jag tänka mig",
                                  "replaces": [f"#U{old}"]})
    assert "replacing #U" in out["content"][0]["text"]
    assert [b["title"] for b in await current(db, subject="bank")] == ["Tekniska roller på bank går bra, inte rådgivning eller sälj"]
    await remember.handler({"statement": "Inga jobbtips den här veckan", "kind": "preference", "area": "communication",
                            "valid_until": "2026-10-25"})
    out = await remember.handler({"statement": "Vill inte söka utbyte", "kind": "preference", "area": "school", "confidence": "low"})
    assert "Kept as" in out["content"][0]["text"]
    out = await remember.handler({"statement": "", "kind": "preference"})
    assert "Not kept" in out["content"][0]["text"]   # every problem goes back to the chat

    mem = tmp_path / "memory"
    mem.mkdir()
    assert await write_profile(db, mem, "Julius")
    profile = (mem / "profile.md").read_text()
    assert "## Inte intresserad av\n- Tekniska roller på bank går bra, inte rådgivning eller sälj (Julius, " in profile
    assert "Vill inte jobba på bank" not in profile   # superseded: in the history, not in the profile
    assert "Vill inte söka utbyte (Julius" in profile and "osäkert" in profile
    assert "gäller till 2026-10-25" in profile or "Inga jobbtips" not in profile   # gone once its date has passed
    assert not await write_profile(db, mem, "Julius")   # unchanged: not written again


async def test_the_chat_is_reminded_of_what_was_said_before_its_turn(db, tmp_path):
    prompts = []

    async def runner(task, ws, on_rl):
        prompts.append(task.prompt)
        return RunResult(text="ok", session_id="s")

    await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (0, 'in', 'Tyvärr måste jag bestämma track 15e oktober')")
    for i in range(25):   # long ago in the conversation
        await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (?, 'out', 'småprat')", (i + 1,))

    async def send(text, candidate_id=None, origin=None, in_session=False):
        return []

    async def typing():
        pass

    chat = ChatSession(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), FakeMemory(), send, typing)
    chat.submit(Incoming(text="Varifrån fick ni att jag skulle välja track den 15 oktober?"))
    await wait_idle(chat)
    assert "[minne:" in prompts[0] and "Julius: Tyvärr måste jag bestämma track 15e oktober" in prompts[0]


async def test_a_big_quiet_conversation_is_folded_into_its_summary_and_starts_fresh(db, tmp_path):
    from agent.chat import SESSION_KEY, SUMMARY_KEY

    tasks = []

    async def runner(task, ws, on_rl):
        tasks.append(task)
        if task.kind == "chat_summary":
            return RunResult(text="", structured={"summary": "Pågående: Google-ansökan, beslut tors."})
        return RunResult(text="", structured={"message": "svar"}, session_id="s2")

    async def send(text, **kw):
        return [await db.execute("INSERT INTO messages (telegram_message_id, direction, text, origin) VALUES (0, 'out', ?, 'chat')", (text,))]

    async def typing():
        pass

    chat = ChatSession(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), FakeMemory(), send, typing)
    await db.kv_set(SESSION_KEY, "s1")
    await db.execute("INSERT INTO claude_runs (kind, context_tokens) VALUES ('chat', 52000)")
    await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (1, 'in', 'Ska jag söka Google?')")
    assert await chat.tidy() is False   # big, but the owner is still here
    await db.execute("UPDATE messages SET created_at = datetime('now', '-2 hours')")
    assert await chat.tidy() is True
    assert tasks[-1].kind == "chat_summary" and "Ska jag söka Google?" in tasks[-1].prompt
    assert await db.kv_get(SESSION_KEY) is None and "Google-ansökan" in await db.kv_get(SUMMARY_KEY)

    row = await db.execute("INSERT INTO messages (telegram_message_id, direction, text) VALUES (2, 'in', 'hej igen')")
    chat.submit(Incoming(text="hej igen", message_row_id=row))
    await wait_idle(chat)
    assert tasks[-1].kind == "chat" and tasks[-1].resume is None
    assert tasks[-1].prompt.count("hej igen") == 1   # the turn's own message isn't repeated among the latest
    assert "Google-ansökan" in tasks[-1].prompt and "Ska jag söka Google?" in tasks[-1].prompt   # summary and the latest messages
    assert await chat.tidy() is False   # a fresh session is small
