import httpx
import pytest

from agent.netguard import PrivateAddress, check_public, public_client
from agent.web import WebReader


@pytest.mark.parametrize("url", [
    "http://localhost:8080/", "http://127.0.0.1/", "http://192.168.1.1/admin", "http://10.0.0.5/", "http://100.79.231.124:22/",
    "http://[::1]/", "http://169.254.169.254/latest/meta-data", "http://raspberrypi.local/", "http://pi.tailnet.ts.net/",
    "file:///etc/passwd", "http://intranet/",
])
async def test_private_and_local_addresses_are_refused(url):
    with pytest.raises(PrivateAddress):
        await check_public(url, resolve=False)


async def test_public_addresses_pass():
    await check_public("https://devpost.com/hackathons", resolve=False)
    await check_public("http://8.8.8.8/", resolve=False)


async def test_redirect_into_the_home_network_is_stopped():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://192.168.1.1/"})

    async with public_client(httpx.MockTransport(handler), follow_redirects=True) as c:
        with pytest.raises(PrivateAddress):
            await c.get("https://evil.example/")


async def test_web_read_refuses_private_urls():
    text, method = await WebReader(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="secret")), reader_service=False).read("http://127.0.0.1:8000/")
    assert method == "none" and "Refused" in text
