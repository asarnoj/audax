import asyncio

import pytest

from agent.chat import SESSION_KEY, ChatSession, Incoming
from agent.claude_gate import ClaudeGate, RunResult
from agent.config import load_settings
from agent.db import Database
from agent import tools as tools_mod
from agent.tools import TurnContext, build_chat_tools


class FakeMemory:
    async def commit_if_changed(self, message):
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
    assert names == ["mcp__agent__record_feedback", "mcp__agent__interview_guide", "mcp__agent__report_problem", "mcp__agent__track_pursuit", "mcp__agent__lookup"]

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

    assert "[you sent proactively" in prompts[0] and "Google-praktik i Stockholm" in prompts[0]
    assert "Google-praktik" not in prompts[1]  # shown once
    assert "SVAR-123" not in prompts[1]  # the chat's own replies are never echoed back



async def test_speak_writes_proactive_message_in_session_and_is_not_echoed_back(db, tmp_path):
    prompts = []

    async def runner(task, ws, on_rl):
        prompts.append(task.prompt)
        reply = "Hackathon på Uni i nov, passar sidoprojektet." if "internal task" in task.prompt else "svar"
        return RunResult(text=reply, session_id="s")

    async def send(text, candidate_id=None, origin=None, in_session=False):
        return [await db.execute("INSERT INTO messages (telegram_message_id, direction, text, origin, in_session) VALUES (0, 'out', ?, ?, ?)", (text, origin, int(in_session)))]

    async def typing():
        pass

    chat = ChatSession(load_settings(), db, ClaudeGate(db, tmp_path, runner=runner), FakeMemory(), send, typing)
    chat.submit(Incoming(text="hej"))  # The owner writes while the dispatcher wants to speak
    ids = await chat.speak("Berätta kort om: Uni-hackathon i november")
    await wait_idle(chat)

    assert len(ids) == 1
    row = await db.fetchone("SELECT origin, in_session, text FROM messages WHERE id = ?", (ids[0],))
    assert (row["origin"], row["in_session"]) == ("proactive", 1)
    assert sum("internal task" in p for p in prompts) == 1  # its own turn, not merged with the owner's message
    chat.submit(Incoming(text="kul"))
    await wait_idle(chat)
    assert "Hackathon på Uni" not in prompts[-1]  # already in the session: not injected again


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
    await tools["send_post"].handler({"verbatim": "Jag har fastnat på hur jag ska nå användare",
                                      "interpretation": "Distributionsproblem, inte tekniskt", "answer_to": f"#P{qid}"})
    [answer] = await posts.inbox("director")  # back to the one who asked
    assert answer["verbatim"].startswith("Jag har fastnat") and answer["question"].startswith("Varför") and answer["kind"] == "answer"
    assert (await posts.get(qid))["status"] == "done" and f"#P{answer['id']}" in turn.refs


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

    sources = instance_config("sources.yaml")
    sources.write_text("streams:\n  - id: hn-best\n    type: rss\n    url: https://hn\n")
    monkeypatch.setattr(ops, "SOURCES_FILE", sources)
    posts = Posts(db)
    tools = {t.name: t for t in _post_tools(posts)}

    out = await tools["change_watch"].handler({"source": "hn", "action": "stop", "why": "behöver inte HN"})
    assert "avstängd" in out["content"][0]["text"]
    assert "enabled: false" in sources.read_text()
    note = (await posts.inbox("director"))[0]
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
    assert "Edit(./memory/profile.md)" in writes and not any("**" in t or "direction" in t or "policy" in t for t in writes)


def test_memory_is_new_until_the_profile_differs_from_the_template(tmp_path):
    from agent.config import ROOT
    from agent.memory import MemoryRepo

    mem = MemoryRepo(tmp_path)
    assert mem.is_new()  # no profile at all
    (tmp_path / "profile.md").write_text((ROOT / "templates" / "memory" / "profile.md").read_text())
    assert mem.is_new()  # still the template: the interview starts on /start
    (tmp_path / "profile.md").write_text("# Profil\n\n## Vem\n- Studerar på KTH\n")
    assert not mem.is_new()
