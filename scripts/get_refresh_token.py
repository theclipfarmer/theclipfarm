#!/usr/bin/env python3
"""One-time helper: get a YouTube refresh token for your channel.

Run this ON YOUR OWN COMPUTER (it opens a browser):

    python3 scripts/get_refresh_token.py YOUR_CLIENT_ID YOUR_CLIENT_SECRET

Sign in with the Google account that owns your clip channel (pick the channel / brand
account when asked). Copy the printed refresh token into the GitHub secret YT_REFRESH_TOKEN.

Standard library only — no pip installs needed.
"""
import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
PORT = 8765
REDIRECT = f"http://127.0.0.1:{PORT}/"


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    client_id, client_secret = sys.argv[1], sys.argv[2]
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent", "state": state,
    })
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] != state or "code" not in q:
                self.send_response(400); self.end_headers()
                self.wfile.write(b"Missing code or bad state. Close this tab and retry.")
                return
            result["code"] = q["code"][0]
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(b"<h2>Done! You can close this tab and go back to the terminal.</h2>")

        def log_message(self, *a):
            pass

    print("Opening your browser to authorise YouTube upload access...\nIf it doesn't open, visit:\n" + url)
    webbrowser.open(url)
    srv = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    while "code" not in result:
        srv.handle_request()

    body = urllib.parse.urlencode({
        "code": result["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": REDIRECT, "grant_type": "authorization_code",
    }).encode()
    with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=body)) as r:
        tok = json.loads(r.read())
    if "refresh_token" not in tok:
        print("No refresh token returned:", tok)
        sys.exit(1)
    print("\n✅ Your refresh token (save it as the GitHub secret YT_REFRESH_TOKEN):\n")
    print(tok["refresh_token"])
    print("\nReminder: set the OAuth consent screen to 'In production', or this token expires in 7 days.")


if __name__ == "__main__":
    main()
