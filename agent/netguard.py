"""Audax reads the public web only: never the Pi itself, the home network or the tailnet.

Pages, mails and models can all suggest URLs, and some of what Audax reads is written by strangers. Every
fetch that can follow such a URL goes through `public_client()`, which checks each hop (redirects too).
The owner's own configured endpoints (calendars, Gmail, GitHub) don't need it.
"""

from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlparse

import httpx

class PrivateAddress(ValueError):
    pass


LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".ts.net")


def _private_ip(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    cgnat = ipaddress.ip_network("100.64.0.0/10")  # includes Tailscale's addresses
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified or (
        ip.version == 4 and ip in cgnat
    )


async def check_public(url: str, resolve: bool = True) -> None:
    """Raises PrivateAddress unless the URL points to the public internet. Audax reads public pages only: never
    the Pi itself, the home network or the tailnet, whatever a page, a mail or a model asks for."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in ("http", "https") or not host:
        raise PrivateAddress(f"not a web address: {url}")
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES) or "." not in host and ":" not in host:
        raise PrivateAddress(f"local address: {host}")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _private_ip(host):
            raise PrivateAddress(f"private address: {host}")
        return
    if resolve:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None)
        except OSError:
            return  # unresolvable: the fetch itself will fail
        if any(_private_ip(i[4][0].split("%")[0]) for i in infos):
            raise PrivateAddress(f"{host} resolves to a private address")


def public_client(transport: httpx.AsyncBaseTransport | None = None, **kwargs) -> httpx.AsyncClient:
    """An httpx client that refuses private addresses on every request, redirects included."""

    async def guard(request: httpx.Request) -> None:
        await check_public(str(request.url), resolve=transport is None)

    return httpx.AsyncClient(transport=transport, event_hooks={"request": [guard]}, **kwargs)
