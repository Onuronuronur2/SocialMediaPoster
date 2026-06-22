#!/usr/bin/env python3
"""TikTok → Instagram Reels auto-crossposter. Kein TikTok API nötig – nutzt yt-dlp."""

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
TIKTOK_USERNAME    = os.environ["TIKTOK_USERNAME"]   # ohne @
TIKTOK_COOKIES_B64 = os.environ["TIKTOK_COOKIES_B64"]

INSTAGRAM_USER_ID  = os.environ["INSTAGRAM_USER_ID"]
INSTAGRAM_APP_ID   = os.environ["INSTAGRAM_APP_ID"]
INSTAGRAM_APP_SECRET = os.environ["INSTAGRAM_APP_SECRET"]

GITHUB_TOKEN       = os.environ["GITHUB_TOKEN"]
GITHUB_REPOSITORY  = os.environ["GITHUB_REPOSITORY"]  # owner/repo

GIST_TOKEN         = os.environ["GIST_TOKEN"]
GIST_ID            = os.environ["GIST_ID"]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

GIST_FILENAME = "state.json"
IG_GRAPH  = "https://graph.instagram.com/v21.0"


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


# ── TikTok via yt-dlp ────────────────────────────────────────────────────────
def write_cookie_file(dest_dir: str) -> str:
    cookie_data = base64.b64decode(TIKTOK_COOKIES_B64).decode()
    cookie_file = str(Path(dest_dir) / "cookies.txt")
    Path(cookie_file).write_text(cookie_data)
    return cookie_file


def get_profile_videos(cookie_file: str, limit: int = 10) -> list[dict]:
    """
    Listet die neuesten Videos vom TikTok-Profil via yt-dlp.
    Gibt Liste von Dicts mit id, description, webpage_url zurück (neueste zuerst).
    """
    profile_url = f"https://www.tiktok.com/@{TIKTOK_USERNAME}"
    ydl_opts = {
        "cookiefile": cookie_file,
        "extract_flat": True,
        "playlist_items": f"1:{limit}",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(profile_url, download=False)

    entries = info.get("entries", []) if info else []
    videos = []
    for e in entries:
        if not e:
            continue
        videos.append({
            "id": e.get("id") or e.get("url", "").split("/")[-1],
            "description": e.get("description") or e.get("title") or "",
            "url": e.get("url") or e.get("webpage_url") or f"https://www.tiktok.com/@{TIKTOK_USERNAME}/video/{e.get('id')}",
        })
    return videos


def download_video(video_url: str, cookie_file: str, dest_dir: str) -> str:
    """
    Lädt TikTok-Inhalt herunter.
    - Video → direkt als MP4
    - Foto-Post (Slideshow) → Thumbnail + Audio werden per ffmpeg zu 7-Sekunden-MP4 kombiniert
    """
    import subprocess

    output_template = str(Path(dest_dir) / "video.%(ext)s")
    ydl_opts = {
        "cookiefile": cookie_file,
        "format": "bestvideo[vcodec^=avc][ext=mp4]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 60,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])

    # Normales Video
    mp4_files = list(Path(dest_dir).glob("video.mp4"))
    if mp4_files:
        return str(mp4_files[0])

    # Foto-Post: Audio + Thumbnail zu MP4 kombinieren
    audio_files = list(Path(dest_dir).glob("video.mp3"))
    thumb_files = [
        f for f in Path(dest_dir).iterdir()
        if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and "video" in f.name
    ]

    if not audio_files:
        raise FileNotFoundError(f"Kein Video/Audio gefunden: {list(Path(dest_dir).iterdir())}")

    audio = str(audio_files[0])
    output_mp4 = str(Path(dest_dir) / "output.mp4")

    if thumb_files:
        log.info("Foto-Post erkannt → kombiniere Thumbnail + Audio zu 7s MP4")
        thumb = str(thumb_files[0])
        subprocess.run([
            "ffmpeg", "-y",
            "-loop", "1", "-i", thumb,
            "-i", audio,
            "-t", "7",
            "-c:v", "libx264", "-tune", "stillimage",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            output_mp4,
        ], check=True, capture_output=True)
    else:
        log.info("Foto-Post ohne Thumbnail → schwarzes Bild + Audio")
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30",
            "-i", audio,
            "-t", "7",
            "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            output_mp4,
        ], check=True, capture_output=True)

    return output_mp4


# ── Temporäres Hosting via GitHub Release ─────────────────────────────────────
def upload_to_github_release(video_path: str, tag: str) -> tuple[str, int]:
    """Erstellt einen GitHub Release, lädt Video hoch. Gibt (url, release_id) zurück."""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    r = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases",
        headers=headers,
        json={"tag_name": tag, "name": f"[auto] {tag}", "prerelease": True, "draft": False},
        timeout=15,
    )
    r.raise_for_status()
    release_id: int = r.json()["id"]
    upload_base: str = r.json()["upload_url"].split("{")[0]

    file_size = Path(video_path).stat().st_size
    log.info(f"Uploading {file_size / 1_000_000:.1f} MB to GitHub Release...")

    with open(video_path, "rb") as f:
        r = requests.post(
            f"{upload_base}?name=video.mp4",
            headers={**headers, "Content-Type": "video/mp4"},
            data=f,
            timeout=300,
        )
    r.raise_for_status()
    return r.json()["browser_download_url"], release_id


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
        log.warning(f"GitHub Release cleanup fehlgeschlagen (nicht kritisch): {e}")


# ── Instagram ─────────────────────────────────────────────────────────────────
def refresh_instagram_token(access_token: str) -> str:
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
        f"{IG_GRAPH}/{INSTAGRAM_USER_ID}/media",
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
    deadline = time.time() + max_wait
    while time.time() < deadline:
        r = requests.get(
            f"{IG_GRAPH}/{container_id}",
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
        f"{IG_GRAPH}/{INSTAGRAM_USER_ID}/media_publish",
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

    try:
        state = read_state()
    except Exception as e:
        msg = f"❌ Gist lesen fehlgeschlagen: {e}"
        log.error(msg)
        telegram(msg)
        sys.exit(1)

    # Instagram Token refreshen
    state["instagram_access_token"] = refresh_instagram_token(state["instagram_access_token"])

    last_id: str | None = state.get("last_video_id")

    with tempfile.TemporaryDirectory() as tmpdir:
        cookie_file = write_cookie_file(tmpdir)

        # Neueste Videos vom Profil holen
        try:
            videos = get_profile_videos(cookie_file)
        except Exception as e:
            msg = f"❌ TikTok Profil-Abruf fehlgeschlagen (Cookies abgelaufen?): {e}"
            log.error(msg)
            telegram(msg)
            sys.exit(1)

        if not videos:
            log.info("Keine Videos gefunden.")
            write_state(state)
            return

        # Erster Run: nur aktuelle ID speichern, nichts posten
        if last_id is None:
            state["last_video_id"] = videos[0]["id"]
            write_state(state)
            log.info(f"Erster Run: Video-ID {videos[0]['id']!r} gespeichert. Ab nächstem neuen Video wird gepostet.")
            telegram("✅ <b>Crossposter initialisiert!</b>\nAb dem nächsten neuen TikTok-Video wird automatisch auf Instagram gepostet.")
            return

        # Neue Videos ermitteln (alles vor last_id, älteste zuerst verarbeiten)
        new_videos = []
        for v in videos:
            if v["id"] == last_id:
                break
            new_videos.append(v)

        if not new_videos:
            log.info(f"Keine neuen Videos seit {last_id}.")
            write_state(state)
            return

        log.info(f"{len(new_videos)} neues Video(s) gefunden")
        new_videos.reverse()  # ältestes zuerst posten

        for video in new_videos:
            video_id = video["id"]
            caption   = video["description"]
            video_url = video["url"]
            log.info(f"Verarbeite Video {video_id}: {caption[:60]!r}")

            release_id: int | None = None
            tag = f"tmp-vid-{video_id}"

            try:
                # Download
                log.info("Lade Video herunter...")
                video_path = download_video(video_url, cookie_file, tmpdir)
                log.info(f"Download OK: {video_path}")

                # Temporär auf GitHub hosten
                log.info("Lade auf GitHub Release hoch...")
                public_url, release_id = upload_to_github_release(video_path, tag)
                log.info(f"Öffentliche URL: {public_url}")

                # Instagram Container + warten + veröffentlichen
                log.info("Erstelle Instagram Media-Container...")
                container_id = create_instagram_container(
                    public_url, caption, state["instagram_access_token"]
                )
                log.info("Warte auf Instagram-Verarbeitung...")
                wait_for_container(container_id, state["instagram_access_token"])
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
