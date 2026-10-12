"""Windows: read-only views of what the owner makes and works on, for the Director (docs/ARCHITECTURE.md).

GitHub: recently active repos with their recent commits and README, so the Director sees what is
being built, how far it has come and where it stands still. Uses GITHUB_TOKEN (fine-grained,
read-only) when set, which also covers private repos; otherwise public data only.
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

API = "https://api.github.com"


async def github_window(user: str | None = None, token: str | None = None, days: int = 120, client: httpx.AsyncClient | None = None) -> str:
    user = user or os.getenv("GITHUB_USER", "").strip()
    token = token if token is not None else os.getenv("GITHUB_TOKEN", "").strip()
    if not user and not token:
        return ""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    client.headers.update(headers)
    try:
        if token:
            r = await client.get(f"{API}/user/repos", params={"sort": "pushed", "per_page": 30, "affiliation": "owner,collaborator"})
        else:
            r = await client.get(f"{API}/users/{user}/repos", params={"sort": "pushed", "per_page": 30})
        r.raise_for_status()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        active = [x for x in r.json() if x.get("pushed_at") and datetime.fromisoformat(x["pushed_at"].replace("Z", "+00:00")) >= cutoff]
        older = [x for x in r.json() if x not in active][:8]

        n_private = sum(1 for x in r.json() if x.get("private"))
        scope = f"{n_private} privata + {len(r.json()) - n_private} publika repon" if token else "bara publika repon"
        lines = [f"GitHub ({scope}). Aktiva senaste {days} dagarna: {len(active)}."]
        for repo in active[:6]:
            name = repo["full_name"]
            commits = await client.get(f"{API}/repos/{name}/commits", params={"per_page": 12})
            msgs = []
            if commits.status_code == 200:
                for c in commits.json():
                    when = c["commit"]["author"]["date"][:10]
                    msgs.append(f"    {when} {c['commit']['message'].splitlines()[0][:100]}")
            readme = ""
            rr = await client.get(f"{API}/repos/{name}/readme")
            if rr.status_code == 200:
                readme = base64.b64decode(rr.json().get("content", "")).decode(errors="replace")
                readme = " ".join(readme.split())[:700]
            lines.append(
                f"- {name}{' (privat)' if repo.get('private') else ''} · {repo.get('language') or '?'} · senast {repo['pushed_at'][:10]}"
                + (f" · {repo['description']}" if repo.get("description") else "")
            )
            if msgs:
                lines.append("  senaste commits:\n" + "\n".join(msgs))
            if readme:
                lines.append(f"  README: {readme}")
        if older:
            lines.append("Äldre repon: " + ", ".join(f"{x['name']} ({x['pushed_at'][:10]})" for x in older))
        return "\n".join(lines)
    except Exception as e:
        log.warning("github window failed: %s", e)
        return f"GitHub: kunde inte läsas ({type(e).__name__})."
    finally:
        if own:
            await client.aclose()


async def all_windows(mail=None) -> str:
    parts = [await github_window()]
    if mail is not None:
        try:
            parts.append(await mail.window())
        except Exception as e:  # a broken mail token must not stop the Director
            log.warning("mail window failed: %s", e)
            parts.append(f"Mail: couldn't be read this time ({type(e).__name__}: {e}).")
    return "\n\n".join(p for p in parts if p) or "No windows connected yet."


# --- GitHub as a read-only tool (chat and Director's reflection) -------------------


class GitHubReader:
    """Read-only access for Claude: list repos, look at one, read a file or directory."""

    MAX_FILE = 12000

    def __init__(self, user: str | None = None, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.user = user or os.getenv("GITHUB_USER", "").strip()
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN", "").strip()
        self._transport = transport  # tests

    def _client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return httpx.AsyncClient(timeout=20, headers=headers, base_url=API, transport=self._transport)

    def _full(self, repo: str) -> str:
        return repo if "/" in repo else f"{self.user}/{repo}"

    async def repos(self) -> str:
        async with self._client() as c:
            r = await c.get("/user/repos" if self.token else f"/users/{self.user}/repos", params={"sort": "pushed", "per_page": 100})
            r.raise_for_status()
        return "\n".join(
            f"- {x['name']}{' (privat)' if x.get('private') else ''} · {x.get('language') or '?'} · senast {x['pushed_at'][:10]}"
            + (f" · {x['description']}" if x.get("description") else "")
            for x in r.json()
        )

    async def repo(self, repo: str, commits: int = 20) -> str:
        name = self._full(repo)
        async with self._client() as c:
            info = await c.get(f"/repos/{name}")
            if info.status_code != 200:
                return f"Hittar inte {name} ({info.status_code})."
            meta = info.json()
            tree = await c.get(f"/repos/{name}/contents/")
            log_ = await c.get(f"/repos/{name}/commits", params={"per_page": commits})
            readme = await c.get(f"/repos/{name}/readme")
        lines = [f"{name}{' (privat)' if meta.get('private') else ''} · {meta.get('language') or '?'} · skapad {meta['created_at'][:10]} · senast {meta['pushed_at'][:10]}"]
        if tree.status_code == 200:
            lines.append("Filer i roten: " + ", ".join(f"{x['name']}{'/' if x['type'] == 'dir' else ''}" for x in tree.json()))
        if log_.status_code == 200:
            lines.append("Senaste commits:")
            lines += [f"  {x['commit']['author']['date'][:10]} {x['commit']['message'].splitlines()[0][:120]}" for x in log_.json()]
        if readme.status_code == 200:
            text = base64.b64decode(readme.json().get("content", "")).decode(errors="replace")
            lines.append("README:\n" + text[:4000])
        return "\n".join(lines)

    async def read(self, repo: str, path: str) -> str:
        name = self._full(repo)
        async with self._client() as c:
            r = await c.get(f"/repos/{name}/contents/{path.lstrip('/')}")
        if r.status_code != 200:
            return f"Hittar inte {path} i {name} ({r.status_code})."
        data = r.json()
        if isinstance(data, list):
            return f"Katalog {path or '/'}:\n" + "\n".join(f"- {x['name']}{'/' if x['type'] == 'dir' else ''}" for x in data)
        if data.get("size", 0) > 500_000:
            return f"{path} är för stor att läsa ({data['size']} byte)."
        text = base64.b64decode(data.get("content", "")).decode(errors="replace")
        return text if len(text) <= self.MAX_FILE else text[: self.MAX_FILE] + f"\n… (avkortad, {len(text)} tecken totalt)"
