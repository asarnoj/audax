"""One-time setup of read-only Gmail access (docs/MAIL.md). Run on a machine with a browser:

    uv run python -m agent.mail_auth path/to/client_secret.json

It opens Google's consent page asking only for gmail.readonly, receives the answer on a local port, and
prints the three lines to put in .env on the Pi. Nothing is written anywhere and nothing leaves this machine
except the exchange with Google.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

from agent.mail import SCOPE, TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def main(path: str) -> None:
    conf = json.load(open(path))
    conf = conf.get("installed") or conf.get("web") or conf
    client_id, client_secret = conf["client_id"], conf["client_secret"]

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state:
                result.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Klart. Du kan stänga fliken och gå tillbaka till terminalen.".encode())

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    print("Öppnar Googles inloggning (bara läsbehörighet). Om inget händer, öppna:\n" + url + "\n")
    webbrowser.open(url)
    while not result:
        server.handle_request()
    if "code" not in result:
        sys.exit(f"Google svarade: {result.get('error', 'okänt fel')}")

    body = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret, "code": result["code"],
        "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect,
    }).encode()
    token = json.load(urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body)))
    if set(token.get("scope", "").split()) != {SCOPE}:
        sys.exit(f"Fel behörighet: {token.get('scope')}. Bara {SCOPE} är tillåten.")
    if "refresh_token" not in token:
        sys.exit("Google gav ingen refresh token. Ta bort appens åtkomst på myaccount.google.com/permissions och kör igen.")
    print("Klart, bara läsbehörighet. Lägg in de här tre raderna i .env på Pi:n:\n")
    print(f"GMAIL_CLIENT_ID={client_id}")
    print(f"GMAIL_CLIENT_SECRET={client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={token['refresh_token']}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
