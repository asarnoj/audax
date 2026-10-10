import base64

import httpx

from agent.windows import github_window


async def test_github_window_summarises_active_repos():
    repos = [
        {"full_name": "a/app", "name": "app", "pushed_at": "2099-01-01T00:00:00Z", "language": "Python", "private": True, "description": "Sidoprojekt"},
        {"full_name": "a/old", "name": "old", "pushed_at": "2020-01-01T00:00:00Z"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/user/repos":
            assert req.headers["authorization"] == "Bearer t"
            return httpx.Response(200, json=repos)
        if path == "/repos/a/app/commits":
            return httpx.Response(200, json=[{"commit": {"author": {"date": "2099-01-01T00:00:00Z"}, "message": "Lägg till quiz\n\ndetaljer"}}])
        if path == "/repos/a/app/readme":
            return httpx.Response(200, json={"content": base64.b64encode(b"# App\nLar dig franska").decode()})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com") as c:
        text = await github_window("a", token="t", client=c)
    assert "1 privata + 1 publika" in text and "a/app (privat)" in text and "Lägg till quiz" in text and "Lar dig franska" in text
    assert "Äldre repon: old" in text


async def test_github_window_without_config_is_empty(monkeypatch):
    monkeypatch.delenv("GITHUB_USER", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert await github_window() == ""


async def test_github_reader_repo_and_read():
    from agent.windows import GitHubReader

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/repos/a/app":
            return httpx.Response(200, json={"private": True, "language": "Python", "created_at": "2026-01-01", "pushed_at": "2026-08-16"})
        if p == "/repos/a/app/contents/":
            return httpx.Response(200, json=[{"name": "src", "type": "dir"}, {"name": "main.py", "type": "file"}])
        if p == "/repos/a/app/contents/main.py":
            return httpx.Response(200, json={"size": 10, "content": base64.b64encode(b"print('hej')").decode()})
        if p == "/repos/a/app/commits":
            return httpx.Response(200, json=[{"commit": {"author": {"date": "2026-08-16T00:00:00Z"}, "message": "Lektionsvyn"}}])
        return httpx.Response(404)

    r = GitHubReader("a", "t", transport=httpx.MockTransport(handler))
    overview = await r.repo("app")
    assert "a/app (privat)" in overview and "src/, main.py" in overview and "Lektionsvyn" in overview
    assert await r.read("app", "main.py") == "print('hej')"
    assert "Hittar inte" in await r.read("app", "nope.py")
