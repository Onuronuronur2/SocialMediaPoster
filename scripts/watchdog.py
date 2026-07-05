#!/usr/bin/env python3
"""
Dead-Man-Switch: Schlägt per Discord Alarm, wenn der Crossposter länger als
MAX_AGE_HOURS keinen Heartbeat (last_updated im Gist) geschrieben hat.
Läuft als eigener Workflow unabhängig vom Crossposter.
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests

GIST_TOKEN          = os.environ["GIST_TOKEN"]
GIST_ID             = os.environ["GIST_ID"]
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

GIST_FILENAME = "state.json"
MAX_AGE_HOURS = 6


def notify(text: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("Discord-Webhook nicht konfiguriert – Alarm kann nicht zugestellt werden!")
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": text[:2000]}, timeout=10)
    except Exception as e:
        print(f"Discord-Fehler: {e}")


def main() -> None:
    headers = {"Authorization": f"token {GIST_TOKEN}"}
    try:
        r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers, timeout=15)
        r.raise_for_status()
        state = json.loads(r.json()["files"][GIST_FILENAME]["content"])
    except Exception as e:
        notify(f"🚨 **Watchdog: State-Gist nicht lesbar!**\n{type(e).__name__}: {e}")
        sys.exit(1)

    last_raw = state.get("last_updated")
    if not last_raw:
        notify("🚨 **Watchdog: kein Heartbeat im State gefunden.**")
        return

    now = datetime.now(timezone.utc)
    age_hours = (now - datetime.fromisoformat(last_raw)).total_seconds() / 3600
    print(f"Letzter Heartbeat vor {age_hours:.1f}h (Limit: {MAX_AGE_HOURS}h)")

    if age_hours < MAX_AGE_HOURS:
        return

    # Max. 1 Alarm pro MAX_AGE_HOURS, sonst spammt der Watchdog alle 2h
    alerted_raw = state.get("watchdog_alerted_at")
    if alerted_raw:
        alert_age = (now - datetime.fromisoformat(alerted_raw)).total_seconds() / 3600
        if alert_age < MAX_AGE_HOURS:
            print("Alarm bereits gesendet – überspringe.")
            return

    notify(
        f"🚨 **Crossposter läuft nicht mehr!**\n"
        f"Letzter Heartbeat vor {age_hours:.1f} Stunden.\n"
        f"Prüfen: cron-job.org Status und GitHub Actions Runs."
    )

    state["watchdog_alerted_at"] = now.isoformat()
    try:
        requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=headers,
            json={"files": {GIST_FILENAME: {"content": json.dumps(state, indent=2)}}},
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        print(f"State-Update fehlgeschlagen: {e}")


if __name__ == "__main__":
    main()
