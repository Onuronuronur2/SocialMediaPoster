#!/usr/bin/env python3
"""
Einmaliges Setup-Script: GitHub Gist für State anlegen.
Lokal ausführen: python scripts/setup.py
"""

import json
import sys
import requests

print("=== TikTok → Instagram Crossposter Setup ===\n")
print("Kein TikTok Developer Account nötig – alles läuft über yt-dlp.\n")

GIST_TOKEN         = input("GitHub PAT (mit 'gist' Scope): ").strip()
instagram_token    = input("Instagram Long-Lived Access Token (siehe Anleitung): ").strip()

initial_state = {
    "last_video_id": None,
    "instagram_access_token": instagram_token,
    "last_updated": None,
}

print("\nErstelle GitHub Gist für State...")
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

print("\n" + "=" * 60)
print("✅ Setup abgeschlossen! Folgende Werte als GitHub Secrets eintragen:\n")
print(f"GIST_TOKEN             = {GIST_TOKEN}")
print(f"GIST_ID                = {gist_id}")
print()
print("Außerdem noch eintragen:")
print("TIKTOK_USERNAME        = <dein TikTok Nutzername ohne @>")
print("TIKTOK_COOKIES_B64     = <base64 deiner cookies.txt>")
print("INSTAGRAM_USER_ID      = <deine Instagram Business Account ID>")
print("INSTAGRAM_APP_ID       = <Meta App ID>")
print("INSTAGRAM_APP_SECRET   = <Meta App Secret>")
print("DISCORD_WEBHOOK_URL    = <optional>")
print("=" * 60)
print(f"\n⚠️  Gist-Link nicht öffentlich teilen: https://gist.github.com/{gist_id}")
