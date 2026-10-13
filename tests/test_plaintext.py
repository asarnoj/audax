from agent.plaintext import to_plain


def test_markdown_becomes_plain_text_for_telegram():
    text = (
        "## Delarna\n- **Director** förstår *vad* som spelar roll.\n* Researcher letar\n"
        "Se [ansökan](https://ssebl.typeform.com/to/B0WyOYNH) och `how_i_work`.\n"
        "| Roll | Fråga |\n|---|---|\n| Manager | När? |\n"
    )
    out = to_plain(text)
    assert "**" not in out and "##" not in out and "`" not in out and "|" not in out
    assert out.startswith("Delarna\n- Director förstår vad som spelar roll.\n- Researcher letar")
    assert "ansökan: https://ssebl.typeform.com/to/B0WyOYNH" in out
    assert "Manager · När?" in out


def test_urls_and_plain_text_are_left_alone():
    text = "Kolla https://example.com/a_b__c_d och 3 * 4 = 12.\n- punkt"
    assert to_plain(text) == text
