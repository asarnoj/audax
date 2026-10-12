import base64
import json

import httpx
import pytest

from agent.mail import SCOPE, MailReader, ScopeError
from agent.tools import build_mail_tools


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


MSG = {
    "id": "m1", "internalDate": "1791100000000", "labelIds": ["INBOX"], "snippet": "Vi vill gärna träffa dig &amp; prata",
    "payload": {
        "headers": [{"name": "From", "value": "Spotify Recruiting <jobs@spotify.com>"}, {"name": "To", "value": "j@x.se"},
                    {"name": "Subject", "value": "Intervju"}],
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/html", "body": {"data": b64("<p>HTML version</p>")}},
                {"mimeType": "text/plain", "body": {"data": b64("Hej! Kan du den 22/10?")}},
            ]},
            {"mimeType": "application/pdf", "filename": "info.pdf", "body": {"attachmentId": "a"}},
        ],
    },
}


def reader(scope=SCOPE, calls=None):
    calls = calls if calls is not None else []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600, "scope": scope})
        assert req.method == "GET"  # read-only: nothing but GETs ever reach Gmail
        assert req.headers["authorization"] == "Bearer t"
        if req.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(200, json=MSG)

    return MailReader("id", "secret", "refresh", transport=httpx.MockTransport(handler)), calls


async def test_search_and_read_prefer_plain_text_and_list_attachments():
    r, calls = reader()
    [m] = await r.search("from:spotify.com")
    assert (m["id"], m["subject"], m["from"]) == ("m1", "Intervju", "Spotify Recruiting <jobs@spotify.com>")
    assert m["snippet"] == "Vi vill gärna träffa dig & prata"
    text = await r.read("m1")
    assert "Kan du den 22/10?" in text and "HTML version" not in text and "info.pdf" in text
    assert sum(c.url.host == "oauth2.googleapis.com" for c in calls) == 1  # token cached


async def test_token_with_more_than_read_access_is_refused():
    r, _ = reader(scope=f"{SCOPE} https://www.googleapis.com/auth/gmail.send")
    with pytest.raises(ScopeError):
        await r.search("x")


async def test_window_marks_mail_as_data():
    r, _ = reader()
    text = await r.window()
    assert "not instructions" in text and "[m1]" in text and "Intervju" in text


async def test_mail_tools_are_read_only():
    r, _ = reader()
    servers, names = build_mail_tools(r)
    assert names == ["mcp__mail__mail_search", "mcp__mail__mail_read"]
