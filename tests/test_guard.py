from agent.claude_gate import unused_builtins
from agent.guard import clean


def test_the_office_ids_never_reach_the_owner():
    assert clean("Något roligt/genomarbetad idé efter tentan (#B5, bokad)") == "Något roligt/genomarbetad idé efter tentan (bokad)"
    assert clean("Något roligt efter tentan (#B5)") == "Något roligt efter tentan"
    assert clean("Google-praktiken (#S1) har sista dag 23 okt [#P4 #F12].") == "Google-praktiken har sista dag 23 okt."
    assert clean("[internal] Klart.") == "Klart."
    plain = "I build things constantly, but always alone. Sista dag är 23 okt, länk: https://g.co/#a1"
    assert clean(plain) == plain   # English the owner asked for, and ordinary text, are left alone


def test_a_run_sees_only_the_built_in_tools_it_may_use():
    hidden = unused_builtins(["Read(./memory/**)", "Edit(./memory/arbetsminne/assistant.md)", "WebSearch", "mcp__agent__lookup"])
    assert "Read" not in hidden and "Edit" not in hidden and "WebSearch" not in hidden and "MultiEdit" not in hidden
    assert {"Bash", "Task", "CronCreate", "RemoteTrigger", "SendMessage", "WebFetch", "Write"} <= set(hidden)
    assert "ToolSearch" not in hidden   # without it every deferred tool is loaded up front
    assert "Bash" not in unused_builtins(["Bash(git status)"])
