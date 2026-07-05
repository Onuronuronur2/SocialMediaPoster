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
import http.cookiejar
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


def process_caption(raw: str) -> str:
    """Instagram-Caption: Text, Footer, max 5 Hashtags (immer am Ende)."""
    caption_text, _ = _extract(raw)
    return f"{caption_text}\n\n{INSTAGRAM_FOOTER}\n\n{_build_hashtags(raw)}"


def youtube_description(raw: str) -> str:
    """YouTube-Beschreibung: Text, Social-Links, Hashtags + #Shorts (immer am Ende)."""
    caption_text, _ = _extract(raw)
    return f"{caption_text}\n\n{YOUTUBE_SOCIALS}\n\n{_build_hashtags(raw, '#Shorts')}"


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


def _download_urls_as_slides(urls: list, work_dir: str) -> list[str]:
    """Lädt URLs als slide_000.jpg, slide_001.jpg usw. herunter."""
    paths = []
    for i, url in enumerate([u for u in urls if u]):
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            if len(r.content) < 500:
                continue
            path = str(Path(work_dir) / f"slide_{i:03d}.jpg")
            Path(path).write_bytes(r.content)
            paths.append(path)
        except Exception as e:
            log.warning(f"Slide {i} fehlgeschlagen: {e}")
    return sorted(paths)


def _extract_json_object(text: str, start: int) -> dict | None:
    """Extrahiert das JSON-Objekt das bei text[start] == '{' beginnt (Brace-Matching)."""
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


def _slides_from_webpage(video_url: str, cookie_file: str, work_dir: str) -> list[str]:
    """
    Primäre Slideshow-Erkennung: lädt die TikTok-Seite und parst das
    eingebettete 'imagePost'-JSON mit allen Slide-Bild-URLs.
    """
    try:
        jar = http.cookiejar.MozillaCookieJar(cookie_file)
        jar.load(ignore_discard=True, ignore_expires=True)

        r = requests.get(
            video_url,
            cookies=jar,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
            },
            timeout=30,
            allow_redirects=True,
        )
        r.raise_for_status()
        html = r.text

        idx = html.find('"imagePost"')
        if idx == -1:
            log.info("Webpage-Check: kein 'imagePost' im HTML → kein Slideshow")
            return []

        brace = html.find("{", idx + len('"imagePost"'))
        image_post = _extract_json_object(html, brace) if brace != -1 else None
        if not image_post or not image_post.get("images"):
            log.warning("Webpage-Check: 'imagePost' gefunden, aber Parsing fehlgeschlagen")
            return []

        urls = []
        for img in image_post["images"]:
            url_list = (img.get("imageURL") or {}).get("urlList") or []
            if url_list:
                urls.append(url_list[0])

        log.info(f"Webpage-Check: imagePost mit {len(urls)} Bildern gefunden")
        return _download_urls_as_slides(urls, work_dir)

    except Exception as e:
        log.warning(f"Webpage-Check fehlgeschlagen: {e}")
        return []


def _detect_slideshow(info: dict, video_url: str, cookie_file: str, work_dir: str) -> list[str]:
    """
    Erkennt TikTok-Slideshow und lädt alle Bilder herunter.
    Primär: imagePost-JSON aus der TikTok-Webseite.
    Fallback: yt-dlp Info-Dict (entries / Bild-Formate / Thumbnails).
    Gibt sortierte Bildpfade zurück, oder [] wenn kein Slideshow.
    """
    # Methode 0 (primär): TikTok-Webseite parsen
    paths = _slides_from_webpage(video_url, cookie_file, work_dir)
    if len(paths) > 1:
        log.info(f"Slideshow (webpage): {len(paths)} Bilder")
        return paths

    if not info:
        return []

    formats    = info.get("formats", [])
    entries    = info.get("entries") or []
    thumbnails = info.get("thumbnails", [])

    log.info(
        f"Slideshow-Check (yt-dlp): _type={info.get('_type', '?')}, "
        f"formats={len(formats)}, entries={len(entries)}, thumbnails={len(thumbnails)}"
    )

    # Methode 1: Playlist – jede Entry ist ein Slide
    if len(entries) > 1:
        urls = []
        for e in entries:
            if not e:
                continue
            entry_fmts = [
                f for f in e.get("formats", [])
                if f.get("url") and (
                    f.get("ext") in ("jpg", "jpeg", "png", "webp")
                    or (f.get("vcodec") == "none" and f.get("acodec") == "none")
                )
            ]
            if entry_fmts:
                urls.append(entry_fmts[-1]["url"])
            else:
                urls.append(e.get("url") or e.get("original_url"))
        paths = _download_urls_as_slides(urls, work_dir)
        if len(paths) > 1:
            log.info(f"Slideshow (entries): {len(paths)} Bilder")
            return paths

    # Methode 2: Bild-Formate mit vcodec=none UND acodec=none
    image_fmts = [
        f for f in formats
        if f.get("vcodec") == "none" and f.get("acodec") == "none" and f.get("url")
    ]
    if len(image_fmts) > 1:
        paths = _download_urls_as_slides([f["url"] for f in image_fmts], work_dir)
        if len(paths) > 1:
            log.info(f"Slideshow (image-formats): {len(paths)} Bilder")
            return paths

    # Methode 3: Formate mit Bild-Extension
    ext_fmts = [
        f for f in formats
        if f.get("ext") in ("jpg", "jpeg", "png", "webp") and f.get("url")
    ]
    if len(ext_fmts) > 1:
        paths = _download_urls_as_slides([f["url"] for f in ext_fmts], work_dir)
        if len(paths) > 1:
            log.info(f"Slideshow (image-ext): {len(paths)} Bilder")
            return paths

    log.info("Kein Slideshow erkannt → normaler Download")
    return []


def _audio_duration(path: str) -> float:
    """Ermittelt die Audio-Länge in Sekunden via ffprobe (0.0 bei Fehler)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _build_slideshow_video(slide_paths: list[str], audio_path: str, work_dir: str) -> str:
    """Stitcht Slide-Bilder + Audio zu einem MP4 (je 2.5s pro Bild).
    Audio wird geloopt falls kürzer als die Slideshow, damit jedes Bild voll gezeigt wird."""
    output_mp4 = str(Path(work_dir) / "output.mp4")
    filelist   = str(Path(work_dir) / "filelist.txt")
    total = len(slide_paths) * 2.5
    with open(filelist, "w") as f:
        for p in slide_paths:
            f.write(f"file '{p}'\n")
            f.write("duration 2.5\n")
        f.write(f"file '{slide_paths[-1]}'\n")

    _ffmpeg([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", filelist,
        "-stream_loop", "-1", "-i", audio_path,
        "-vf", (
            "scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
        ),
        "-c:v", "libx264", "-crf", "18", "-preset", "slow",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-t", f"{total}",
        output_mp4,
    ])
    return output_mp4


def download_video(video_url: str, cookie_file: str, work_dir: str) -> str:
    """
    Lädt TikTok-Inhalt herunter:
    - Normales Video  → video.mp4
    - Einzelnes Foto  → Bild + kompletter Sound → MP4 in Sound-Länge
    - Slideshow       → alle Slides + Audio → je 2.5s pro Bild
    Slideshow wird VOR dem Haupt-Download erkannt, damit kein falsches Einzelbild-Video entsteht.
    """
    # ── Schritt 1: Info holen (ohne Download) → Slideshow-Erkennung ──────────
    with yt_dlp.YoutubeDL({
        "cookiefile": cookie_file, "quiet": True,
        "no_warnings": True, "socket_timeout": 30,
    }) as ydl:
        info = ydl.extract_info(video_url, download=False)

    slide_paths = _detect_slideshow(info or {}, video_url, cookie_file, work_dir)

    # ── Schritt 2a: Slideshow ─────────────────────────────────────────────────
    if len(slide_paths) > 1:
        log.info(f"Slideshow: {len(slide_paths)} Bilder à 2.5s – lade Audio separat...")
        audio_tpl = str(Path(work_dir) / "audio.%(ext)s")
        with yt_dlp.YoutubeDL({
            "cookiefile": cookie_file,
            "format": "bestaudio",
            "outtmpl": audio_tpl,
            "quiet": True, "no_warnings": True, "socket_timeout": 60,
        }) as ydl:
            ydl.download([video_url])

        audio_files = list(Path(work_dir).glob("audio.*"))
        if not audio_files:
            raise FileNotFoundError("Kein Audio für Slideshow gefunden")
        return _build_slideshow_video(slide_paths, str(audio_files[0]), work_dir)

    # ── Schritt 2b: Normaler Download (Video oder Einzelfoto) ────────────────
    out_tpl  = str(Path(work_dir) / "video.%(ext)s")
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

    if (Path(work_dir) / "video.mp4").exists():
        return str(Path(work_dir) / "video.mp4")

    audio_files = list(Path(work_dir).glob("video.mp3"))
    if not audio_files:
        raise FileNotFoundError(f"Kein Video/Audio: {[f.name for f in Path(work_dir).iterdir()]}")
    audio      = str(audio_files[0])
    output_mp4 = str(Path(work_dir) / "output.mp4")

    # ── Einzelnes Foto ────────────────────────────────────────────────────────
    thumb_files = [
        f for f in Path(work_dir).iterdir()
        if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
    ]

    # Sound-Länge bestimmt die Video-Länge (min. 3s wegen Instagram-Minimum)
    duration = max(3.0, _audio_duration(audio))
    log.info(f"Einzelfoto → MP4 in Sound-Länge ({duration:.1f}s)")

    if thumb_files:
        _ffmpeg([
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(thumb_files[0]),
            "-i", audio,
            "-c:v", "libx264", "-tune", "stillimage",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-t", f"{duration}",
            output_mp4,
        ])
    else:
        log.info("Kein Bild gefunden → schwarzer Hintergrund")
        _ffmpeg([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30",
            "-i", audio,
            "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-t", f"{duration}",
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
              "caption": caption, "share_to_feed": False},
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
        log.error(f"YouTube Token-Refresh fehlgeschlagen: {r.text}")
        if "invalid_grant" in r.text:
            telegram(
                "❌ <b>YouTube Refresh-Token abgelaufen!</b>\n"
                "Ursache: OAuth-App steht auf 'Testing' → Token läuft nach 7 Tagen ab.\n"
                "Fix: console.cloud.google.com → OAuth consent screen → App auf "
                "'In Production' stellen, dann setup_youtube.py neu ausführen und "
                "YOUTUBE_REFRESH_TOKEN Secret aktualisieren."
            )
        else:
            telegram(f"❌ <b>YouTube Token-Fehler:</b>\n{r.text[:300]}")
        return None
    log.info("YouTube Token erfolgreich geladen")
    return r.json()["access_token"]


def upload_to_youtube(video_path: str, title: str, description: str, token: str) -> str:
    file_size = Path(video_path).stat().st_size
    # YouTube-Titel: keine < >, keine Zeilenumbrüche, max 100 Zeichen
    clean_title = title.replace("<", "").replace(">", "").replace("\n", " ").strip()
    yt_title = (clean_title[:90] + " #Shorts") if clean_title else "#Shorts"

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
    if r.status_code >= 400:
        raise RuntimeError(f"YouTube Upload-Init {r.status_code}: {r.text[:500]}")
    upload_url = r.headers["Location"]

    log.info(f"YouTube Upload: {file_size / 1_000_000:.1f} MB")
    with open(video_path, "rb") as f:
        r = requests.put(
            upload_url,
            headers={"Content-Type": "video/mp4", "Content-Length": str(file_size)},
            data=f,
            timeout=300,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"YouTube Upload {r.status_code}: {r.text[:500]}")
    result = r.json()

    # uploadStatus prüfen – 'rejected' heißt: hochgeladen aber nicht sichtbar
    upload_status = (result.get("status") or {}).get("uploadStatus", "")
    if upload_status == "rejected":
        reason = (result.get("status") or {}).get("rejectionReason", "unbekannt")
        raise RuntimeError(f"YouTube hat das Video abgelehnt: {reason}")

    return result["id"]


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

                caption    = process_caption(raw_caption)
                yt_desc    = youtube_description(raw_caption)
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
                else:
                    log.warning("YouTube-Upload übersprungen: kein gültiges Token (siehe Fehler oben)")

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
