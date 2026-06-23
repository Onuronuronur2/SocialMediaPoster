#!/usr/bin/env python3
"""TikTok → Instagram Reels + YouTube Shorts auto-crossposter."""

import os
import sys
import json
import time
import base64
import logging
import shutil
import subprocess
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
TIKTOK_USERNAME      = os.environ["TIKTOK_USERNAME"]
TIKTOK_COOKIES_B64   = os.environ["TIKTOK_COOKIES_B64"]

INSTAGRAM_USER_ID    = os.environ["INSTAGRAM_USER_ID"]
INSTAGRAM_APP_ID     = os.environ["INSTAGRAM_APP_ID"]
INSTAGRAM_APP_SECRET = os.environ["INSTAGRAM_APP_SECRET"]

GITHUB_TOKEN         = os.environ["GITHUB_TOKEN"]
GITHUB_REPOSITORY    = os.environ["GITHUB_REPOSITORY"]

GIST_TOKEN           = os.environ["GIST_TOKEN"]
GIST_ID              = os.environ["GIST_ID"]

TELEGRAM_BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")

YOUTUBE_CLIENT_ID     = os.environ.get("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "")
GEMINI_DAILY_LIMIT  = 1400  # kostenloses Limit: 1.500/Tag – wir bleiben 100 darunter

_gemini_quota_exceeded = False  # wird auf True gesetzt sobald 429 kommt → restliche Calls überspringen

GIST_FILENAME = "state.json"
IG_GRAPH      = "https://graph.instagram.com/v21.0"

# ── Caption ───────────────────────────────────────────────────────────────────
HASHTAG_POOL = ["#gym", "#sport", "#fitness", "#deutsch", "#meme", "#durchziehen"]

INSTAGRAM_FOOTER = (
    "🔥 Mehr Content von mir:\n"
    "📱 TikTok: @onursportlich\n"
    "▶️ YouTube: @onursportlich"
)

YOUTUBE_SOCIALS = (
    "🔥 Mehr Content von mir:\n"
    "📱 TikTok (@onursportlich): https://www.tiktok.com/@onursportlich\n"
    "📸 Instagram (@onursportlich): https://www.instagram.com/onursportlich/"
)


def _extract(raw: str) -> tuple[str, list[str]]:
    """Gibt (caption_text, unique_hashtags) zurück."""
    words = raw.split()
    hashtags   = [w for w in words if w.startswith("#")]
    text_words = [w for w in words if not w.startswith("#")]
    caption_text = " ".join(text_words).strip()

    seen: set[str] = set()
    unique: list[str] = []
    for h in hashtags:
        if h.lower() not in seen:
            seen.add(h.lower())
            unique.append(h)
    return caption_text, unique


def _build_hashtags(raw: str, suffix: str = "") -> str:
    _, unique = _extract(raw)
    for tag in HASHTAG_POOL:
        if len(unique) >= 5:
            break
        if tag.lower() not in {h.lower() for h in unique}:
            unique.append(tag)
    tags = " ".join(unique[:5])
    return f"{tags} {suffix}".strip() if suffix else tags


def build_instagram_caption(main_text: str, raw: str) -> str:
    return f"{main_text}\n\n{INSTAGRAM_FOOTER}\n\n{_build_hashtags(raw)}"


def build_youtube_description(main_text: str, raw: str) -> str:
    return f"{main_text}\n\n{YOUTUBE_SOCIALS}\n\n{_build_hashtags(raw, '#Shorts')}"


def process_caption(raw: str) -> str:
    caption_text, _ = _extract(raw)
    return build_instagram_caption(caption_text, raw)


def youtube_description(raw: str) -> str:
    caption_text, _ = _extract(raw)
    return build_youtube_description(caption_text, raw)


def _gemini_quota_ok(state: dict) -> bool:
    """Prüft ob noch genug Gemini-Requests übrig sind (min. 2 für IG + YT)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("gemini_date") != today:
        state["gemini_calls"] = 0
        state["gemini_date"] = today
    used = state.get("gemini_calls", 0)
    remaining = GEMINI_DAILY_LIMIT - used
    log.info(f"Gemini Quota: {used}/{GEMINI_DAILY_LIMIT} heute genutzt ({remaining} übrig)")
    return remaining >= 2


def generate_caption_gemini(video_path: str, raw_caption: str, platform: str) -> str:
    """Lässt Gemini das Video analysieren und schreibt eine plattformgerechte Caption.
    Gibt bei Fehler den bereinigten Original-Text zurück (kein Absturz, kein Kosten-Risiko)."""
    global _gemini_quota_exceeded

    if _gemini_quota_exceeded:
        log.info(f"Gemini ({platform}) übersprungen – Quota in diesem Run erschöpft")
        caption_text, _ = _extract(raw_caption)
        return caption_text

    client = None
    video_file = None
    try:
        from google import genai as google_genai

        client = google_genai.Client(api_key=GEMINI_API_KEY)

        log.info(f"Gemini: Video hochladen ({platform})...")
        video_file = client.files.upload(file=video_path)

        while video_file.state.name == "PROCESSING":
            time.sleep(5)
            video_file = client.files.get(name=video_file.name)

        if video_file.state.name != "ACTIVE":
            raise RuntimeError(f"Gemini File-Status: {video_file.state.name}")

        if platform == "instagram":
            prompt = (
                f"Du analysierst ein Video für Instagram Reels (@onursportlich).\n"
                f"Kanal: Fitness, Sport, deutsche Memes – junges deutsches Publikum.\n"
                f"Originaler TikTok-Text: \"{raw_caption}\"\n\n"
                f"Schreibe NUR den Caption-Haupttext auf Deutsch (1-2 kurze Sätze).\n"
                f"Ton: locker, authentisch, zum Video passend. Keine Hashtags, kein Footer."
            )
        else:
            prompt = (
                f"Du analysierst ein Video für YouTube Shorts (@onursportlich).\n"
                f"Kanal: Fitness, Sport, deutsche Memes – junges deutsches Publikum.\n"
                f"Originaler TikTok-Text: \"{raw_caption}\"\n\n"
                f"Schreibe NUR die Beschreibung auf Deutsch (2-3 Sätze).\n"
                f"Etwas informativer als Instagram, aber locker. Keine Hashtags, kein Footer."
            )

        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=[video_file, prompt],
        )

        try:
            client.files.delete(name=video_file.name)
        except Exception:
            pass

        text = response.text.strip()
        log.info(f"Gemini {platform}-Caption: {text[:80]!r}")
        return text

    except Exception as e:
        # Hochgeladene Datei aufräumen falls vorhanden
        if client is not None and video_file is not None:
            try:
                client.files.delete(name=video_file.name)
            except Exception:
                pass

        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            _gemini_quota_exceeded = True
            log.warning("Gemini Quota erschöpft → alle weiteren Gemini-Calls in diesem Run übersprungen")
        else:
            log.warning(f"Gemini ({platform}) fehlgeschlagen: {e} → Fallback auf Original")

        caption_text, _ = _extract(raw_caption)
        return caption_text


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
        log.warning(f"Telegram: {e}")


# ── State (Gist) ──────────────────────────────────────────────────────────────
def read_state() -> dict:
    r = requests.get(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"token {GIST_TOKEN}"},
        timeout=10,
    )
    r.raise_for_status()
    return json.loads(r.json()["files"][GIST_FILENAME]["content"])


def write_state(state: dict) -> None:
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"token {GIST_TOKEN}"},
        json={"files": {GIST_FILENAME: {"content": json.dumps(state, indent=2)}}},
        timeout=10,
    ).raise_for_status()


# ── TikTok via yt-dlp ────────────────────────────────────────────────────────
def write_cookie_file(dest: str) -> str:
    path = str(Path(dest) / "cookies.txt")
    Path(path).write_text(base64.b64decode(TIKTOK_COOKIES_B64).decode())
    return path


def get_profile_videos(cookie_file: str, limit: int = 10) -> list[dict]:
    ydl_opts = {
        "cookiefile": cookie_file,
        "extract_flat": True,
        "playlist_items": f"1:{limit}",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"https://www.tiktok.com/@{TIKTOK_USERNAME}", download=False)

    videos = []
    for e in (info or {}).get("entries", []):
        if not e:
            continue
        vid_id = e.get("id") or e.get("url", "").split("/")[-1]
        videos.append({
            "id": vid_id,
            "description": e.get("description") or e.get("title") or "",
            "url": e.get("url") or e.get("webpage_url")
                   or f"https://www.tiktok.com/@{TIKTOK_USERNAME}/video/{vid_id}",
        })
    return videos


def _ffmpeg(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg fehlgeschlagen: {result.stderr.decode()}")


def _download_slide_images(video_url: str, cookie_file: str, work_dir: str) -> list[str]:
    """
    Versucht alle Slide-Bilder einer TikTok-Slideshow herunterzuladen.
    Gibt sortierte Liste von Bildpfaden zurück (leer wenn kein Slideshow).
    """
    ydl_info_opts = {
        "cookiefile": cookie_file,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
    }
    with yt_dlp.YoutubeDL(ydl_info_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)

    thumbnails = info.get("thumbnails", []) if info else []
    # TikTok Slideshow-Bilder haben typischerweise eine eigene URL pro Slide
    # Wir filtern auf Bilder mit signifikanter Auflösung (keine Mini-Previews)
    slides = [
        t for t in thumbnails
        if t.get("url") and (t.get("width", 0) >= 200 or t.get("height", 0) >= 200)
    ]

    if len(slides) <= 1:
        return []  # Kein Slideshow oder nur Cover

    paths = []
    for i, slide in enumerate(slides):
        url = slide["url"]
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            path = str(Path(work_dir) / f"slide_{i:03d}.jpg")
            Path(path).write_bytes(r.content)
            paths.append(path)
        except Exception as e:
            log.warning(f"Slide {i} konnte nicht geladen werden: {e}")

    return sorted(paths)


def download_video(video_url: str, cookie_file: str, work_dir: str) -> str:
    """
    Lädt TikTok-Inhalt herunter:
    - Normales Video      → video.mp4
    - Einzelnes Foto      → Thumbnail + Audio → 7s MP4
    - Slideshow           → alle Slides + Audio → je 2.5s pro Bild
    """
    out_tpl = str(Path(work_dir) / "video.%(ext)s")
    ydl_opts = {
        "cookiefile": cookie_file,
        "format": "bestvideo+bestaudio/best",
        "outtmpl": out_tpl,
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "convert_thumbnails": "jpg",
        "postprocessor_args": {"ffmpeg": ["-crf", "18", "-preset", "slow"]},
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 60,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])

    log.info(f"Dateien: {[f.name for f in Path(work_dir).iterdir()]}")

    # ── Normales Video ────────────────────────────────────────────────────────
    if (Path(work_dir) / "video.mp4").exists():
        return str(Path(work_dir) / "video.mp4")

    # ── Foto / Slideshow: Audio muss vorhanden sein ───────────────────────────
    audio_files = list(Path(work_dir).glob("video.mp3"))
    if not audio_files:
        raise FileNotFoundError(
            f"Kein Video/Audio: {[f.name for f in Path(work_dir).iterdir()]}"
        )
    audio = str(audio_files[0])
    output_mp4 = str(Path(work_dir) / "output.mp4")

    # ── Slideshow: alle Slides herunterladen ──────────────────────────────────
    slide_paths = _download_slide_images(video_url, cookie_file, work_dir)

    if len(slide_paths) > 1:
        log.info(f"Slideshow erkannt: {len(slide_paths)} Bilder à 2.5s")
        filelist = str(Path(work_dir) / "filelist.txt")
        with open(filelist, "w") as f:
            for p in slide_paths:
                f.write(f"file '{p}'\n")
                f.write("duration 2.5\n")
            f.write(f"file '{slide_paths[-1]}'\n")  # letztes Bild nochmal für ffmpeg-concat

        _ffmpeg([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", filelist,
            "-i", audio,
            "-vf", (
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
            ),
            "-c:v", "libx264", "-crf", "18", "-preset", "slow",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            output_mp4,
        ])
        return output_mp4

    # ── Einzelnes Foto ────────────────────────────────────────────────────────
    thumb_files = [
        f for f in Path(work_dir).iterdir()
        if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
        and f.name != "filelist.txt"
    ]

    if thumb_files:
        log.info(f"Einzelfoto → 7s MP4")
        _ffmpeg([
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(thumb_files[0]),
            "-i", audio,
            "-t", "7",
            "-c:v", "libx264", "-tune", "stillimage",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            output_mp4,
        ])
    else:
        log.info("Foto ohne Bild → schwarzes Bild + Audio")
        _ffmpeg([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30",
            "-i", audio,
            "-t", "7",
            "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            output_mp4,
        ])

    return output_mp4


# ── GitHub Release (temp hosting für Instagram) ───────────────────────────────
def upload_to_github_release(video_path: str, tag: str) -> tuple[str, int]:
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
    log.info(f"GitHub Release Upload: {file_size / 1_000_000:.1f} MB")
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
        log.warning(f"GitHub Release cleanup: {e}")


# ── Instagram ─────────────────────────────────────────────────────────────────
def refresh_instagram_token(token: str) -> str:
    r = requests.get(
        f"{IG_GRAPH}/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token},
        timeout=15,
    )
    if r.status_code != 200:
        log.warning(f"Instagram Token-Refresh fehlgeschlagen: {r.text}")
        return token
    return r.json()["access_token"]


def create_instagram_container(video_url: str, caption: str, token: str) -> str:
    r = requests.post(
        f"{IG_GRAPH}/{INSTAGRAM_USER_ID}/media",
        params={"access_token": token},
        json={"media_type": "REELS", "video_url": video_url,
              "caption": caption, "share_to_feed": True},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Instagram Container fehlgeschlagen: {r.text}")
    return r.json()["id"]


def wait_for_container(container_id: str, token: str, max_wait: int = 600) -> None:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        r = requests.get(
            f"{IG_GRAPH}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=15,
        )
        r.raise_for_status()
        status = r.json().get("status_code")
        log.info(f"Container {container_id}: {status}")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError(f"Instagram Container-Fehler: {r.json()}")
        time.sleep(20)
    raise TimeoutError(f"Container {container_id} Timeout nach {max_wait}s")


def publish_instagram_reel(container_id: str, token: str) -> str:
    r = requests.post(
        f"{IG_GRAPH}/{INSTAGRAM_USER_ID}/media_publish",
        params={"access_token": token},
        json={"creation_id": container_id},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Instagram Publish fehlgeschlagen: {r.text}")
    return r.json()["id"]


# ── YouTube Shorts ────────────────────────────────────────────────────────────
def get_youtube_token() -> str | None:
    if not (YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET and YOUTUBE_REFRESH_TOKEN):
        log.info("YouTube: Secrets nicht konfiguriert → übersprungen")
        return None
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": YOUTUBE_CLIENT_ID,
            "client_secret": YOUTUBE_CLIENT_SECRET,
            "refresh_token": YOUTUBE_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    if r.status_code != 200:
        log.warning(f"YouTube Token-Refresh fehlgeschlagen: {r.text}")
        return None
    log.info("YouTube Token erfolgreich geladen")
    return r.json()["access_token"]


def upload_to_youtube(video_path: str, title: str, description: str, token: str) -> str:
    file_size = Path(video_path).stat().st_size
    yt_title = (title[:95] + " #Shorts") if title else "#Shorts"

    metadata = json.dumps({
        "snippet": {
            "title": yt_title,
            "description": description,
            "categoryId": "17",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    })

    r = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(file_size),
        },
        data=metadata,
        timeout=30,
    )
    r.raise_for_status()
    upload_url = r.headers["Location"]

    log.info(f"YouTube Upload: {file_size / 1_000_000:.1f} MB")
    with open(video_path, "rb") as f:
        r = requests.put(
            upload_url,
            headers={"Content-Type": "video/mp4", "Content-Length": str(file_size)},
            data=f,
            timeout=300,
        )
    r.raise_for_status()
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

    state["instagram_access_token"] = refresh_instagram_token(state["instagram_access_token"])
    yt_token = get_youtube_token()
    last_id: str | None = state.get("last_video_id")

    with tempfile.TemporaryDirectory() as base_dir:
        cookie_file = write_cookie_file(base_dir)

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

        # Erster Run
        if last_id is None:
            state["last_video_id"] = videos[0]["id"]
            write_state(state)
            log.info(f"Erster Run: ID {videos[0]['id']!r} gespeichert.")
            telegram("✅ <b>Crossposter initialisiert!</b>\nAb dem nächsten TikTok-Video wird automatisch gepostet.")
            return

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
        new_videos.reverse()

        posted_ids: list[str] = state.setdefault("posted_ids", [])

        for video in new_videos:
            video_id    = video["id"]
            raw_caption = video["description"]
            video_url   = video["url"]
            log.info(f"Verarbeite {video_id}: {raw_caption[:60]!r}")

            if video_id in posted_ids:
                log.info(f"Duplikat übersprungen: {video_id}")
                continue

            # Eigenes Verzeichnis pro Video → keine Konflikte
            work_dir = str(Path(base_dir) / video_id)
            Path(work_dir).mkdir()

            release_id: int | None = None
            tag = f"tmp-vid-{video_id}-{int(time.time())}"

            try:
                log.info("Lade Video herunter...")
                video_path = download_video(video_url, cookie_file, work_dir)
                log.info(f"Download OK: {video_path}")

                # Caption generieren
                # Footer & Hashtags werden IMMER angehängt (build_instagram_caption / build_youtube_description)
                # Gemini liefert nur den Haupttext – bei Quota-Erschöpfung oder Fehler: Fallback
                if GEMINI_API_KEY and _gemini_quota_ok(state):
                    ig_text = generate_caption_gemini(video_path, raw_caption, "instagram")
                    yt_text = generate_caption_gemini(video_path, raw_caption, "youtube")
                    state["gemini_calls"] = state.get("gemini_calls", 0) + 2
                    caption = build_instagram_caption(ig_text, raw_caption)
                    yt_desc = build_youtube_description(yt_text, raw_caption)
                    title_text = ig_text.split("\n")[0][:95] or raw_caption.split("#")[0].strip() or "New Short"
                else:
                    if GEMINI_API_KEY:
                        log.info("Gemini Quota erschöpft → Fallback auf Original-Caption")
                    caption = process_caption(raw_caption)
                    yt_desc = youtube_description(raw_caption)
                    title_text = raw_caption.split("#")[0].strip() or "New Short"

                log.info("Lade auf GitHub Release hoch...")
                public_url, release_id = upload_to_github_release(video_path, tag)
                log.info(f"URL: {public_url}")

                log.info("Instagram Container erstellen...")
                container_id = create_instagram_container(
                    public_url, caption, state["instagram_access_token"]
                )
                log.info("Warte auf Instagram-Verarbeitung...")
                wait_for_container(container_id, state["instagram_access_token"])
                ig_id = publish_instagram_reel(container_id, state["instagram_access_token"])
                log.info(f"✅ Instagram: {ig_id}")

                # YouTube – video_path noch vorhanden (work_dir existiert noch)
                yt_video_id = None
                if yt_token:
                    try:
                        yt_video_id = upload_to_youtube(video_path, title_text, yt_desc, yt_token)
                        log.info(f"✅ YouTube Short: {yt_video_id}")
                    except Exception as e:
                        log.error(f"YouTube fehlgeschlagen: {e}", exc_info=True)
                        telegram(f"⚠️ YouTube fehlgeschlagen für {video_id}: {e}")

                state["last_video_id"] = video_id
                posted_ids.append(video_id)
                state["posted_ids"] = posted_ids[-100:]  # max 100 IDs behalten
                write_state(state)

                yt_line = f"\nYouTube: <code>{yt_video_id}</code>" if yt_video_id else ""
                telegram(
                    f"✅ <b>Neuer Post!</b>\n"
                    f"Instagram: <code>{ig_id}</code>{yt_line}\n"
                    f"Caption: {raw_caption[:100]}"
                )

            except Exception as e:
                log.error(f"Fehler bei {video_id}: {e}", exc_info=True)
                telegram(f"❌ <b>Fehler bei {video_id}</b>\n{type(e).__name__}: {e}")
            finally:
                if release_id is not None:
                    delete_github_release(release_id, tag)
                # Work-Dir aufräumen
                shutil.rmtree(work_dir, ignore_errors=True)

    log.info("=== Run beendet ===")


if __name__ == "__main__":
    main()
