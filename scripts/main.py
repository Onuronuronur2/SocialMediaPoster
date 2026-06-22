#!/usr/bin/env python3
"""TikTok → Instagram Reels auto-crossposter."""

import os
import sys
import json
import time
import base64
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests
import yt_dlp

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TIKTOK_CLIENT_KEY    = os.environ["TIKTOK_CLIENT_KEY"]
TIKTOK_CLIENT_SECRET = os.environ["TIKTOK_CLIENT_SECRET"]
TIKTOK_USERNAME      = os.environ["TIKTOK_USERNAME"]  # ohne @
TIKTOK_COOKIES_B64   = os.environ["TIKTOK_COOKIES_B64"]

INSTAGRAM_USER_ID    = os.environ["INSTAGRAM_USER_ID"]
INSTAGRAM_APP_ID     = os.environ["INSTAGRAM_APP_ID"]
INSTAGRAM_APP_SECRET = os.environ["INSTAGRAM_APP_SECRET"]

GITHUB_TOKEN         = os.environ["GITHUB_TOKEN"]
GITHUB_REPOSITORY    = os.environ["GITHUB_REPOSITORY"]  # owner/repo

GIST_TOKEN           = os.environ["GIST_TOKEN"]
GIST_ID              = os.environ["GIST_ID"]

TELEGRAM_BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")

GIST_FILENAME = "state.json"
GRAPH_API = "https://graph.facebook.com/v21.0"
IG_GRAPH = "https://graph.instagram.com"


# ── Telegram ──────────────────────────────────────────────────────────────────
def telegram(text: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


# ── State (Gist) ──────────────────────────────────────────────────────────────
def read_state() -> dict:
    r = requests.get(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"token {GIST_TOKEN}"},
        timeout=10,
    )
    r.raise_for_status()
    content = r.json()["files"][GIST_FILENAME]["content"]
    return json.loads(content)


def write_state(state: dict) -> None:
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"token {GIST_TOKEN}"},
        json={"files": {GIST_FILENAME: {"content": json.dumps(state, indent=2)}}},
        timeout=10,
    ).raise_for_status()


# ── TikTok ────────────────────────────────────────────────────────────────────
def refresh_tiktok_token(refresh_token: str) -> tuple[str, str]:
    """Returns (access_token, new_refresh_token). TikTok rotiert refresh tokens."""
    r = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError(f"TikTok token refresh error: {data}")
    return data["access_token"], data["refresh_token"]


def get_new_videos(access_token: str, last_known_id: str | None) -> list[dict]:
    """
    Holt TikTok-Videos und gibt nur neuere als last_known_id zurück (älteste zuerst).
    Bei erstem Run (last_known_id=None): gibt [] zurück und state wird nur initialisiert.
    """
    collected = []
    cursor = 0
    has_more = True
    found_anchor = False

    for _ in range(5):  # max 50 Videos durchsuchen
        if not has_more:
            break

        payload: dict = {"max_count": 10}
        if cursor:
            payload["cursor"] = cursor

        r = requests.post(
            "https://open.tiktokapis.com/v2/video/list/",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "id,video_description,create_time,share_url"},
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        batch = data.get("videos", [])

        for v in batch:
            if v["id"] == last_known_id:
                found_anchor = True
                break
            collected.append(v)

        if found_anchor or not last_known_id:
            break

        cursor = data.get("cursor", 0)
        has_more = data.get("has_more", False)

    if not last_known_id:
        return []

    if not found_anchor and collected:
        log.warning(
            f"last_known_id {last_known_id!r} nicht in API-Response gefunden. "
            "Möglicherweise wurden seit dem letzten Run sehr viele Videos gepostet."
        )

    # API liefert neueste zuerst → umkehren für chronologische Reihenfolge
    return list(reversed(collected))


def get_latest_video_id(access_token: str) -> str | None:
    """Nur für den allerersten Run: aktuellste Video-ID holen ohne zu posten."""
    r = requests.post(
        "https://open.tiktokapis.com/v2/video/list/",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"fields": "id,create_time"},
        json={"max_count": 1},
        timeout=15,
    )
    r.raise_for_status()
    videos = r.json().get("data", {}).get("videos", [])
    return videos[0]["id"] if videos else None


# ── yt-dlp Download ───────────────────────────────────────────────────────────
def download_video(video_id: str, dest_dir: str) -> str:
    """Lädt TikTok-Video mit Cookies herunter (wasserzeichenfrei für eigene Videos)."""
    share_url = f"https://www.tiktok.com/@{TIKTOK_USERNAME}/video/{video_id}"

    cookie_data = base64.b64decode(TIKTOK_COOKIES_B64).decode()
    cookie_file = Path(dest_dir) / "cookies.txt"
    cookie_file.write_text(cookie_data)

    output_template = str(Path(dest_dir) / "video.%(ext)s")
    ydl_opts = {
        "cookiefile": str(cookie_file),
        "format": "bestvideo[vcodec^=avc][ext=mp4]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([share_url])

    mp4_files = list(Path(dest_dir).glob("video.mp4"))
    if not mp4_files:
        all_files = list(Path(dest_dir).glob("video.*"))
        raise FileNotFoundError(
            f"Kein MP4 nach Download in {dest_dir}, gefunden: {all_files}"
        )
    return str(mp4_files[0])


# ── Temporäres Hosting via GitHub Release ─────────────────────────────────────
# Instagram benötigt eine öffentlich erreichbare Video-URL.
# Wir erstellen einen temporären GitHub Release (prerelease), laden das Video hoch,
# und löschen ihn nach dem Instagram-Post wieder.

def upload_to_github_release(video_path: str, tag: str) -> tuple[str, int]:
    """Gibt (download_url, release_id) zurück."""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    r = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases",
        headers=headers,
        json={
            "tag_name": tag,
            "name": f"[auto] {tag}",
            "prerelease": True,
            "draft": False,
        },
        timeout=15,
    )
    r.raise_for_status()
    release_id: int = r.json()["id"]
    upload_base: str = r.json()["upload_url"].split("{")[0]

    filename = "video.mp4"
    file_size = Path(video_path).stat().st_size
    log.info(f"Uploading {file_size / 1_000_000:.1f} MB to GitHub Release...")

    with open(video_path, "rb") as f:
        r = requests.post(
            f"{upload_base}?name={filename}",
            headers={**headers, "Content-Type": "video/mp4"},
            data=f,
            timeout=300,
        )
    r.raise_for_status()
    download_url: str = r.json()["browser_download_url"]
    return download_url, release_id


def delete_github_release(release_id: int, tag: str) -> None:
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    try:
        requests.delete(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/{release_id}",
            headers=headers, timeout=10,
        )
        requests.delete(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/git/refs/tags/{tag}",
            headers=headers, timeout=10,
        )
    except Exception as e:
        log.warning(f"GitHub Release cleanup failed (nicht kritisch): {e}")


# ── Instagram ─────────────────────────────────────────────────────────────────
def refresh_instagram_token(access_token: str) -> str:
    """
    Refreshed einen Instagram Long-Lived Token (gültig 60 Tage, refreshbar solange aktiv).
    Verwendet den Instagram Graph API Refresh-Endpunkt (nicht Facebook).
    """
    r = requests.get(
        f"{IG_GRAPH}/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": access_token},
        timeout=15,
    )
    if r.status_code != 200:
        log.warning(f"Instagram Token-Refresh fehlgeschlagen (weiter mit altem): {r.text}")
        return access_token
    return r.json()["access_token"]


def create_instagram_container(video_url: str, caption: str, access_token: str) -> str:
    r = requests.post(
        f"{GRAPH_API}/{INSTAGRAM_USER_ID}/media",
        params={"access_token": access_token},
        json={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": True,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Instagram Container-Erstellung fehlgeschlagen: {r.text}")
    return r.json()["id"]


def wait_for_container(container_id: str, access_token: str, max_wait: int = 600) -> None:
    """Pollt bis Container-Status FINISHED ist."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        r = requests.get(
            f"{GRAPH_API}/{container_id}",
            params={"fields": "status_code,status", "access_token": access_token},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        status = data.get("status_code")
        log.info(f"Container {container_id}: {status}")

        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError(f"Instagram Container-Fehler: {data}")
        time.sleep(20)

    raise TimeoutError(f"Container {container_id} nach {max_wait}s noch nicht FINISHED")


def publish_instagram_reel(container_id: str, access_token: str) -> str:
    r = requests.post(
        f"{GRAPH_API}/{INSTAGRAM_USER_ID}/media_publish",
        params={"access_token": access_token},
        json={"creation_id": container_id},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Instagram Publish fehlgeschlagen: {r.text}")
    return r.json()["id"]


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=== Run gestartet ===")

    # State lesen
    try:
        state = read_state()
    except Exception as e:
        msg = f"❌ Gist lesen fehlgeschlagen: {e}"
        log.error(msg)
        telegram(msg)
        sys.exit(1)

    # TikTok Token refreshen
    try:
        tiktok_token, new_refresh = refresh_tiktok_token(state["tiktok_refresh_token"])
        state["tiktok_refresh_token"] = new_refresh
        log.info("TikTok Token refresht")
    except Exception as e:
        msg = f"❌ TikTok Token-Refresh fehlgeschlagen: {e}"
        log.error(msg)
        telegram(msg)
        sys.exit(1)

    # Instagram Token refreshen
    new_ig_token = refresh_instagram_token(state["instagram_access_token"])
    state["instagram_access_token"] = new_ig_token

    last_id: str | None = state.get("last_video_id")

    # Erster Run: nur aktuellste ID speichern, nichts posten
    if last_id is None:
        latest_id = get_latest_video_id(tiktok_token)
        state["last_video_id"] = latest_id
        write_state(state)
        log.info(f"Erster Run: aktuellste Video-ID {latest_id!r} gespeichert. Ab nächstem Run wird gepostet.")
        telegram("✅ <b>Crossposter initialisiert.</b>\nAb dem nächsten neuen TikTok-Video wird automatisch auf Instagram gepostet.")
        return

    # Neue Videos holen
    try:
        new_videos = get_new_videos(tiktok_token, last_id)
    except Exception as e:
        msg = f"❌ TikTok Video-Liste fehlgeschlagen: {e}"
        log.error(msg)
        telegram(msg)
        sys.exit(1)

    if not new_videos:
        log.info("Keine neuen Videos.")
        write_state(state)
        return

    log.info(f"{len(new_videos)} neues Video(s) gefunden")

    for video in new_videos:
        video_id = video["id"]
        caption = video.get("video_description") or ""
        log.info(f"Verarbeite Video {video_id}: {caption[:60]!r}")

        release_id: int | None = None
        tag = f"tmp-vid-{video_id}"

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                # Download
                log.info("Lade Video via yt-dlp herunter...")
                video_path = download_video(video_id, tmpdir)
                log.info(f"Download erfolgreich: {video_path}")

                # Temporär auf GitHub hosten
                log.info("Lade auf GitHub Release hoch...")
                video_url, release_id = upload_to_github_release(video_path, tag)
                log.info(f"Öffentliche URL: {video_url}")

            # Instagram Container erstellen
            log.info("Erstelle Instagram Media-Container...")
            container_id = create_instagram_container(
                video_url, caption, state["instagram_access_token"]
            )

            # Warten bis verarbeitet
            log.info("Warte auf Instagram-Verarbeitung...")
            wait_for_container(container_id, state["instagram_access_token"])

            # Veröffentlichen
            media_id = publish_instagram_reel(container_id, state["instagram_access_token"])
            log.info(f"✅ Instagram Reel veröffentlicht: {media_id}")

            state["last_video_id"] = video_id
            write_state(state)

            telegram(
                f"✅ <b>Neues Reel gepostet!</b>\n"
                f"TikTok-ID: <code>{video_id}</code>\n"
                f"Instagram-ID: <code>{media_id}</code>\n"
                f"Caption: {caption[:150]}"
            )

        except Exception as e:
            log.error(f"Fehler bei Video {video_id}: {e}", exc_info=True)
            telegram(f"❌ <b>Fehler bei Video {video_id}</b>\n{type(e).__name__}: {e}")
        finally:
            if release_id is not None:
                log.info("Lösche temporären GitHub Release...")
                delete_github_release(release_id, tag)

    log.info("=== Run beendet ===")


if __name__ == "__main__":
    main()
