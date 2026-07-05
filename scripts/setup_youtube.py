#!/usr/bin/env python3
"""
Einmaliges Setup für YouTube OAuth.
Lokal ausführen: python3 scripts/setup_youtube.py
"""

import json
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import requests

print("=== YouTube OAuth Setup ===\n")
CLIENT_ID     = input("Google Client ID: ").strip()
CLIENT_SECRET = input("Google Client Secret: ").strip()

REDIRECT_URI = "http://localhost:8080/callback"
# upload = Videos hochladen | youtube = Titel ändern (A/B-Test) | analytics = CTR lesen
SCOPE = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube "
    "https://www.googleapis.com/auth/yt-analytics.readonly"
)
STATE = secrets.token_urlsafe(16)

code_holder: dict = {}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code_holder["code"] = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<h2>YouTube autorisiert! Du kannst dieses Fenster schliessen.</h2>")
    def log_message(self, *args): pass

server = HTTPServer(("localhost", 8080), Handler)
thread = Thread(target=server.handle_request)
thread.start()

auth_url = (
    "https://accounts.google.com/o/oauth2/v2/auth?"
    + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": STATE,
    })
)

print("\nÖffne Google-Autorisierung im Browser...")
webbrowser.open(auth_url)
thread.join(timeout=120)
server.server_close()

code = code_holder.get("code")
if not code:
    print("❌ Kein Code erhalten.")
    sys.exit(1)

r = requests.post(
    "https://oauth2.googleapis.com/token",
    data={
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    },
)
r.raise_for_status()
data = r.json()

print("\n" + "=" * 60)
print("✅ YouTube Setup abgeschlossen! Als GitHub Secrets eintragen:\n")
print(f"YOUTUBE_CLIENT_ID      = {CLIENT_ID}")
print(f"YOUTUBE_CLIENT_SECRET  = {CLIENT_SECRET}")
print(f"YOUTUBE_REFRESH_TOKEN  = {data['refresh_token']}")
print("=" * 60)
