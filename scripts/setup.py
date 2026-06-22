#!/usr/bin/env python3
"""
Einmaliges Setup-Script: TikTok OAuth + Gist erstellen.
Lokal ausführen: python scripts/setup.py
"""

import json
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import requests

# ── Eingaben ──────────────────────────────────────────────────────────────────
print("=== TikTok → Instagram Crossposter Setup ===\n")

CLIENT_KEY    = input("TikTok Client Key: ").strip()
CLIENT_SECRET = input("TikTok Client Secret: ").strip()
GIST_TOKEN    = input("GitHub PAT (mit 'gist' Scope): ").strip()

REDIRECT_URI = "http://localhost:8080/callback"
SCOPE = "user.info.basic,video.list"
STATE = secrets.token_urlsafe(16)

# ── TikTok OAuth ──────────────────────────────────────────────────────────────
auth_code_holder: dict = {}

class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        auth_code_holder["code"] = params.get("code", [None])[0]
        auth_code_holder["state"] = params.get("state", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<h2>Autorisierung erfolgreich! Du kannst dieses Fenster schliessen.</h2>")

    def log_message(self, *args):
        pass

server = HTTPServer(("localhost", 8080), CallbackHandler)
thread = Thread(target=server.handle_request)
thread.start()

auth_url = (
    "https://www.tiktok.com/v2/auth/authorize/?"
    + urllib.parse.urlencode({
        "client_key": CLIENT_KEY,
        "scope": SCOPE,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": STATE,
    })
)

print(f"\nÖffne TikTok-Autorisierung im Browser...")
webbrowser.open(auth_url)
print(f"Falls der Browser sich nicht öffnet: {auth_url}\n")

thread.join(timeout=120)
server.server_close()

code = auth_code_holder.get("code")
if not code:
    print("❌ Kein Auth-Code erhalten. Abbruch.")
    sys.exit(1)

print("✅ Auth-Code erhalten. Tausche gegen Token...")

# Code gegen Tokens tauschen
r = requests.post(
    "https://open.tiktokapis.com/v2/oauth/token/",
    data={
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    },
    timeout=15,
)
r.raise_for_status()
token_data = r.json()

if "access_token" not in token_data:
    print(f"❌ Token-Austausch fehlgeschlagen: {token_data}")
    sys.exit(1)

tiktok_access_token  = token_data["access_token"]
tiktok_refresh_token = token_data["refresh_token"]
print(f"✅ TikTok Tokens erhalten. Refresh Token läuft in {token_data.get('refresh_expires_in', '?')}s ab.")

# ── GitHub Gist erstellen ─────────────────────────────────────────────────────
print("\nErstelle GitHub Gist für State...")

instagram_token = input(
    "\nInstagram Long-Lived Access Token (aus Graph API Explorer, siehe Setup-Anleitung): "
).strip()

initial_state = {
    "last_video_id": None,
    "tiktok_refresh_token": tiktok_refresh_token,
    "instagram_access_token": instagram_token,
    "last_updated": None,
}

r = requests.post(
    "https://api.github.com/gists",
    headers={"Authorization": f"token {GIST_TOKEN}"},
    json={
        "description": "TikTok→Instagram Crossposter State",
        "public": False,
        "files": {
            "state.json": {"content": json.dumps(initial_state, indent=2)}
        },
    },
    timeout=10,
)
r.raise_for_status()
gist_id = r.json()["id"]

# ── Ausgabe ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("✅ Setup abgeschlossen! Folgende Werte als GitHub Secrets eintragen:\n")
print(f"TIKTOK_CLIENT_KEY      = {CLIENT_KEY}")
print(f"TIKTOK_CLIENT_SECRET   = {CLIENT_SECRET}")
print(f"GIST_TOKEN             = {GIST_TOKEN}")
print(f"GIST_ID                = {gist_id}")
print()
print("Außerdem noch eintragen (manuell besorgen, siehe Anleitung):")
print("TIKTOK_USERNAME        = <dein TikTok Nutzername ohne @>")
print("TIKTOK_COOKIES_B64     = <base64 deiner cookies.txt>")
print("INSTAGRAM_USER_ID      = <deine Instagram Business Account ID>")
print("INSTAGRAM_APP_ID       = <Meta App ID>")
print("INSTAGRAM_APP_SECRET   = <Meta App Secret>")
print("TELEGRAM_BOT_TOKEN     = <optional>")
print("TELEGRAM_CHAT_ID       = <optional>")
print("=" * 60)
print("\n⚠️  Den Gist-Link NICHT öffentlich teilen (enthält Tokens):")
print(f"   https://gist.github.com/{gist_id}")
