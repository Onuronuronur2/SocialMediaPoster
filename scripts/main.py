#!/usr/bin/env python3
"""TikTok → Instagram Reels + YouTube Shorts auto-crossposter."""

import os
import re
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

DISCORD_WEBHOOK_URL  = os.environ.get("DISCORD_WEBHOOK_URL", "")

YOUTUBE_CLIENT_ID     = os.environ.get("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

ARCHIVE_REPO   = os.environ.get("ARCHIVE_REPO", "")    # z.B. "User/TikTok-Archiv" (privates Repo)
ARCHIVE_TOKEN  = os.environ.get("ARCHIVE_TOKEN", "")   # PAT mit repo-Scope für das Archiv-Repo
POSTING_WINDOW = os.environ.get("POSTING_WINDOW", "")  # z.B. "17-21" (Europe/Berlin), leer = sofort posten

RETRY_MAX = 3  # max. Versuche pro Video bevor es endgültig aufgegeben wird

PRIVATE_RECHECK_DAYS = 3  # private Videos so lange erneut prüfen (falls sie öffentlich werden)

# Performance-Monitoring: 7 Tage beobachten, ab 2000 Views → KPI-Log
PERF_THRESHOLD_VIEWS = 2000
PERF_WATCH_DAYS      = 7
PERF_CHECK_HOURS     = 4    # Mindestabstand zwischen zwei Checks desselben Posts

# YouTube A/B-Test: 24h Titel A, 24h Titel B, dann gewinnt der bessere
AB_PHASE_HOURS = 24
AB_HOOKS       = ["🔥", "😳", "💀", "⚡"]

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


# ── HTTP mit Retry ────────────────────────────────────────────────────────────
def _retry_request(method: str, url: str, *, attempts: int = 3, **kwargs) -> requests.Response:
    """HTTP-Request mit Retry bei transienten Fehlern (Timeout, Connection-Reset, 5xx).
    4xx-Antworten werden direkt zurückgegeben – das ist Sache des Aufrufers."""
    kwargs.setdefault("timeout", 30)
    last_err = ""
    for i in range(attempts):
        try:
            r = requests.request(method, url, **kwargs)
            if r.status_code < 500:
                return r
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = f"{type(e).__name__}: {e}"
        if i < attempts - 1:
            wait = 5 * (i + 1)
            log.warning(f"{method} {url.split('?')[0]} fehlgeschlagen ({last_err}) → Retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"{method} {url.split('?')[0]} nach {attempts} Versuchen fehlgeschlagen: {last_err}")


# ── Discord ───────────────────────────────────────────────────────────────────
def notify(text: str) -> None:
    """Sendet eine Nachricht an den Discord-Webhook (Markdown, max 2000 Zeichen)."""
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": text[:2000]}, timeout=10)
    except Exception as e:
        log.warning(f"Discord: {e}")


# ── State (Gist) ──────────────────────────────────────────────────────────────
def read_state() -> dict:
    r = _retry_request(
        "GET",
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"token {GIST_TOKEN}"},
    )
    r.raise_for_status()
    return json.loads(r.json()["files"][GIST_FILENAME]["content"])


def write_state(state: dict) -> None:
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    _retry_request(
        "PATCH",
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"token {GIST_TOKEN}"},
        json={"files": {GIST_FILENAME: {"content": json.dumps(state, indent=2)}}},
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


_TIKTOK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


def _load_cookie_jar(cookie_file: str):
    jar = http.cookiejar.MozillaCookieJar(cookie_file)
    jar.load(ignore_discard=True, ignore_expires=True)
    return jar


def _fetch_tiktok_html(video_url: str, cookie_file: str) -> str:
    """
    Holt das TikTok-Seiten-HTML. Probiert auch die /photo/-Variante der URL –
    Slideshows liefern dort zuverlässiger ihre imagePost-Daten.
    Leerstring wenn nichts brauchbares kommt.
    """
    candidates = [video_url]
    if "/video/" in video_url:
        candidates.append(video_url.replace("/video/", "/photo/"))

    try:
        jar = _load_cookie_jar(cookie_file)
    except Exception as e:
        log.warning(f"Cookie-Jar nicht ladbar: {e}")
        jar = None

    best = ""
    for url in candidates:
        try:
            r = _retry_request("GET", url, cookies=jar, headers=_TIKTOK_HEADERS,
                               attempts=2, allow_redirects=True)
            if r.status_code != 200:
                continue
            html = r.text
            # Enthält Slideshow-Daten → sofort nehmen
            if '"imagePost"' in html or '"image_post_info"' in html:
                return html
            if len(html) > len(best):
                best = html
        except Exception as e:
            log.warning(f"TikTok-HTML von {url} fehlgeschlagen: {e}")
    return best


def tiktok_privacy(html: str, info: dict) -> str:
    """Ermittelt die Sichtbarkeit: 'private' | 'public' | 'unknown'.
    Primär aus dem Seiten-HTML (privateItem-Flag), Fallback yt-dlp availability."""
    if html:
        if '"privateItem":true' in html:
            return "private"
        if '"privateItem":false' in html:
            return "public"
    availability = (info or {}).get("availability")
    if availability in ("private", "needs_auth", "subscriber_only"):
        return "private"
    if availability == "public":
        return "public"
    return "unknown"


def _slides_from_html(html: str, work_dir: str) -> list[str]:
    """
    Parst Slide-Bild-URLs aus dem TikTok-HTML (nur für Foto-Posts).
    3 Parser: imagePost (camelCase), image_post_info (snake_case), Regex-Fallback.
    """
    if not html:
        return []
    is_photo_post = '"imagePost"' in html or '"image_post_info"' in html
    if not is_photo_post:
        log.info("HTML-Check: kein imagePost → kein Foto-Post/Slideshow")
        return []

    urls: list[str] = []

    # Parser 1: webapp-Format {"imagePost":{"images":[{"imageURL":{"urlList":[...]}}]}}
    idx = html.find('"imagePost"')
    if idx != -1:
        brace = html.find("{", idx + len('"imagePost"'))
        obj = _extract_json_object(html, brace) if brace != -1 else None
        for img in (obj or {}).get("images", []):
            url_list = (img.get("imageURL") or {}).get("urlList") or []
            if url_list:
                urls.append(url_list[0])

    # Parser 2: API-Format {"image_post_info":{"images":[{"display_image":{"url_list":[...]}}]}}
    if not urls:
        idx = html.find('"image_post_info"')
        if idx != -1:
            brace = html.find("{", idx + len('"image_post_info"'))
            obj = _extract_json_object(html, brace) if brace != -1 else None
            for img in (obj or {}).get("images", []):
                url_list = (img.get("display_image") or {}).get("url_list") or []
                if url_list:
                    urls.append(url_list[0])

    # Parser 3: Regex-Fallback, nur im imagePost-Segment (sonst falsche Treffer von Covern)
    if not urls:
        seg_start = max(html.find('"imagePost"'), html.find('"image_post_info"'))
        segment = html[seg_start:seg_start + 100_000]
        for match in re.finditer(r'"(?:urlList|url_list)":\s*\[\s*"((?:[^"\\]|\\.)+)"', segment):
            try:
                urls.append(json.loads(f'"{match.group(1)}"'))
            except Exception:
                pass

    urls = list(dict.fromkeys(urls))  # Duplikate raus, Reihenfolge behalten
    log.info(f"HTML-Check: Foto-Post mit {len(urls)} Bild-URL(s)")
    return _download_urls_as_slides(urls, work_dir)


def _detect_slideshow(info: dict, video_url: str, cookie_file: str,
                      work_dir: str, html: str = "") -> list[str]:
    """
    Erkennt TikTok-Slideshow und lädt alle Bilder herunter.
    Primär: imagePost-JSON aus der TikTok-Webseite (html wird mitgegeben oder geholt).
    Fallback: yt-dlp Info-Dict (entries / Bild-Formate).
    Gibt sortierte Bildpfade zurück, oder [] wenn kein Slideshow.
    """
    # Methode 0 (primär): TikTok-Webseite parsen
    if not html:
        html = _fetch_tiktok_html(video_url, cookie_file)
    paths = _slides_from_html(html, work_dir)
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


def _log_video_specs(path: str) -> None:
    """Loggt Codec, Auflösung und Bitrate der fertigen Datei (Qualitätskontrolle)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,bit_rate",
             "-of", "json", path],
            capture_output=True, text=True,
        )
        stream = json.loads(result.stdout)["streams"][0]
        bitrate_kbit = int(stream.get("bit_rate") or 0) / 1000
        size_mb = Path(path).stat().st_size / 1_000_000
        log.info(
            f"📹 Video-Specs: {stream.get('codec_name')} "
            f"{stream.get('width')}x{stream.get('height')}, "
            f"{bitrate_kbit:.0f} kbit/s, {size_mb:.1f} MB"
        )
    except Exception as e:
        log.warning(f"Specs-Check fehlgeschlagen: {e}")


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


def _video_info(video_url: str, cookie_file: str) -> dict:
    """Voll-Metadaten eines einzelnen Videos (Caption, Formate, Thumbnails)."""
    with yt_dlp.YoutubeDL({
        "cookiefile": cookie_file, "quiet": True,
        "no_warnings": True, "socket_timeout": 30,
    }) as ydl:
        return ydl.extract_info(video_url, download=False) or {}


def download_video(video_url: str, cookie_file: str, work_dir: str,
                   info: dict | None = None, html: str = "") -> str:
    """
    Lädt TikTok-Inhalt herunter:
    - Normales Video  → video.mp4
    - Einzelnes Foto  → Bild + kompletter Sound → MP4 in Sound-Länge
    - Slideshow       → alle Slides + Audio → je 2.5s pro Bild
    Slideshow wird VOR dem Haupt-Download erkannt, damit kein falsches Einzelbild-Video entsteht.
    """
    # ── Schritt 1: Info holen (falls nicht mitgegeben) → Slideshow-Erkennung ──
    if info is None:
        info = _video_info(video_url, cookie_file)

    slide_paths = _detect_slideshow(info, video_url, cookie_file, work_dir, html=html)

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
    # format_sort: höchste Auflösung, dann H.264 vor H.265 (höhere Bitrate bei
    # TikTok + Instagram verarbeitet H.264 am besten), dann höchste Bitrate.
    # Kein Re-Encode: der Stream wird 1:1 übernommen wie TikTok ihn liefert.
    out_tpl  = str(Path(work_dir) / "video.%(ext)s")
    ydl_opts = {
        "cookiefile": cookie_file,
        "format": "bestvideo+bestaudio/best",
        "format_sort": ["res", "vcodec:h264", "tbr"],
        "outtmpl": out_tpl,
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "convert_thumbnails": "jpg",
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
    # slide_* (Full-Res von der TikTok-Seite) bevorzugen, yt-dlp-Thumbnail nur als Fallback
    thumb_files = sorted(
        (f for f in Path(work_dir).iterdir()
         if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")),
        key=lambda f: (0 if f.name.startswith("slide_") else 1, f.name),
    )

    # Letzte Rettung gegen schwarzen Screen: bestes Thumbnail aus den Metadaten laden
    if not thumb_files:
        thumbs = sorted(
            (t for t in info.get("thumbnails", []) if t.get("url")),
            key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
        )
        if thumbs:
            log.info("Kein lokales Bild → lade bestes Thumbnail aus Metadaten")
            paths = _download_urls_as_slides([thumbs[-1]["url"]], work_dir)
            thumb_files = [Path(p) for p in paths]

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
def refresh_instagram_token(state: dict) -> str:
    token = state["instagram_access_token"]
    try:
        r = _retry_request(
            "GET",
            f"{IG_GRAPH}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": token},
        )
    except Exception as e:
        # Netzwerkproblem → alten Token behalten, Run nicht abbrechen
        log.warning(f"Instagram Token-Refresh nicht erreichbar: {e}")
        return token
    if r.status_code != 200:
        log.warning(f"Instagram Token-Refresh fehlgeschlagen: {r.text}")
        # Warnung max. 1x/Tag – wenn das dauerhaft fehlschlägt, stirbt der Token nach 60 Tagen
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state.get("ig_refresh_warned_date") != today:
            state["ig_refresh_warned_date"] = today
            notify(
                f"⚠️ **Instagram Token-Refresh fehlgeschlagen.**\n"
                f"Wenn das täglich kommt: neuen Long-Lived-Token erstellen und im Gist "
                f"(`instagram_access_token`) ersetzen.\n{r.text[:300]}"
            )
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
    try:
        r = _retry_request(
            "POST",
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": YOUTUBE_CLIENT_ID,
                "client_secret": YOUTUBE_CLIENT_SECRET,
                "refresh_token": YOUTUBE_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
        )
    except Exception as e:
        # Netzwerkproblem → YouTube diesen Run überspringen statt alles abzubrechen
        log.warning(f"YouTube Token-Endpoint nicht erreichbar: {e}")
        return None
    if r.status_code != 200:
        log.error(f"YouTube Token-Refresh fehlgeschlagen: {r.text}")
        if "invalid_grant" in r.text:
            notify(
                "❌ **YouTube Refresh-Token abgelaufen!**\n"
                "Ursache: OAuth-App steht auf 'Testing' → Token läuft nach 7 Tagen ab.\n"
                "Fix: console.cloud.google.com → OAuth consent screen → App auf "
                "'In Production' stellen, dann setup_youtube.py neu ausführen und "
                "YOUTUBE_REFRESH_TOKEN Secret aktualisieren."
            )
        else:
            notify(f"❌ **YouTube Token-Fehler:**\n{r.text[:300]}")
        return None
    log.info("YouTube Token erfolgreich geladen")
    return r.json()["access_token"]


def upload_to_youtube(video_path: str, title: str, description: str, token: str) -> tuple[str, str]:
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

    return result["id"], yt_title


# ── Robustheit & Extras ───────────────────────────────────────────────────────
def in_posting_window() -> bool:
    """True wenn jetzt gepostet werden darf (POSTING_WINDOW leer = immer)."""
    if not POSTING_WINDOW:
        return True
    try:
        from zoneinfo import ZoneInfo
        start_h, end_h = (int(x) for x in POSTING_WINDOW.split("-"))
        hour = datetime.now(ZoneInfo("Europe/Berlin")).hour
        if start_h <= end_h:
            return start_h <= hour < end_h
        return hour >= start_h or hour < end_h  # Fenster über Mitternacht, z.B. "22-2"
    except Exception as e:
        log.warning(f"POSTING_WINDOW ungültig ({POSTING_WINDOW!r}): {e} → poste sofort")
        return True


def check_cookie_expiry(cookie_file: str, state: dict) -> None:
    """Warnt per Discord wenn wichtige TikTok-Session-Cookies in <7 Tagen ablaufen (max. 1x/Tag)."""
    try:
        now = time.time()
        soonest: tuple[float, str] | None = None
        for line in Path(cookie_file).read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7 or "tiktok" not in parts[0]:
                continue
            expiry_raw, name = parts[4], parts[5]
            if name not in ("sessionid", "sessionid_ss", "sid_tt", "sid_guard"):
                continue
            try:
                expiry = float(expiry_raw)
            except ValueError:
                continue
            if expiry <= 0:
                continue
            if soonest is None or expiry < soonest[0]:
                soonest = (expiry, name)

        if soonest is None:
            return
        days_left = (soonest[0] - now) / 86400
        if days_left > 7:
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state.get("cookie_warned_date") == today:
            return
        state["cookie_warned_date"] = today

        if days_left < 0:
            msg = (f"❌ **TikTok-Cookie '{soonest[1]}' ist abgelaufen!**\n"
                   f"Neue Cookies exportieren und Secret TIKTOK_COOKIES_B64 aktualisieren.")
        else:
            msg = (f"⚠️ **TikTok-Cookie '{soonest[1]}' läuft in {days_left:.0f} Tag(en) ab.**\n"
                   f"Bald neue Cookies exportieren und Secret TIKTOK_COOKIES_B64 aktualisieren.")
        log.warning(msg)
        notify(msg)
    except Exception as e:
        log.warning(f"Cookie-Check fehlgeschlagen: {e}")


def archive_video(video_path: str, video_id: str, caption: str) -> None:
    """Sichert das Video dauerhaft als Release-Asset im privaten Archiv-Repo (optional)."""
    if not (ARCHIVE_REPO and ARCHIVE_TOKEN):
        return
    headers = {"Authorization": f"token {ARCHIVE_TOKEN}", "Accept": "application/vnd.github+json"}
    tag = f"tiktok-{video_id}"
    try:
        r = requests.get(
            f"https://api.github.com/repos/{ARCHIVE_REPO}/releases/tags/{tag}",
            headers=headers, timeout=15,
        )
        if r.status_code == 200:
            log.info(f"Archiv: {video_id} bereits gesichert")
            return

        r = requests.post(
            f"https://api.github.com/repos/{ARCHIVE_REPO}/releases",
            headers=headers,
            json={"tag_name": tag, "name": (caption[:80] or tag), "body": caption},
            timeout=15,
        )
        r.raise_for_status()
        upload_base = r.json()["upload_url"].split("{")[0]
        with open(video_path, "rb") as f:
            r = requests.post(
                f"{upload_base}?name={video_id}.mp4",
                headers={**headers, "Content-Type": "video/mp4"},
                data=f, timeout=300,
            )
        r.raise_for_status()
        log.info(f"✅ Archiv: {video_id} gesichert")
    except Exception as e:
        log.warning(f"Archiv-Backup fehlgeschlagen (nicht kritisch): {e}")


# ── Performance-Monitoring (pro Plattform unabhängig) ────────────────────────
def _fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _kpi_line(stats: dict) -> str:
    mapping = [("views", "👀 Views"), ("reach", "📣 Reach"), ("likes", "❤️ Likes"),
               ("comments", "💬 Kommentare"), ("shares", "🔁 Shares"), ("saved", "🔖 Saves")]
    return " | ".join(f"{label}: {_fmt_int(stats[key])}" for key, label in mapping if key in stats)


def _ig_media_stats(media_id: str, token: str) -> dict | None:
    r = None
    for metrics in ("views,reach,likes,comments,shares,saved",
                    "plays,reach,likes,comments,shares,saved"):
        r = requests.get(
            f"{IG_GRAPH}/{media_id}/insights",
            params={"metric": metrics, "access_token": token},
            timeout=15,
        )
        if r.status_code == 200:
            out = {}
            for item in r.json().get("data", []):
                values = item.get("values") or [{}]
                out[item["name"]] = values[0].get("value", 0) or 0
            if "plays" in out and "views" not in out:
                out["views"] = out.pop("plays")
            return out
    log.warning(f"IG Insights fehlgeschlagen für {media_id}: {r.text[:200] if r is not None else '?'}")
    return None


def _yt_video_stats(video_id: str, token: str) -> dict | None:
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": "statistics", "id": video_id},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if r.status_code != 200:
        log.warning(f"YT Stats fehlgeschlagen für {video_id}: {r.text[:200]}")
        return None
    items = r.json().get("items", [])
    if not items:
        return None
    s = items[0].get("statistics", {})
    return {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
            "comments": int(s.get("commentCount", 0))}


def _tiktok_video_stats(url: str, cookie_file: str) -> dict | None:
    try:
        with yt_dlp.YoutubeDL({"cookiefile": cookie_file, "quiet": True,
                               "no_warnings": True, "socket_timeout": 30}) as ydl:
            info = ydl.extract_info(url, download=False)
        return {"views": info.get("view_count") or 0, "likes": info.get("like_count") or 0,
                "comments": info.get("comment_count") or 0, "shares": info.get("repost_count") or 0}
    except Exception as e:
        log.warning(f"TikTok Stats fehlgeschlagen: {e}")
        return None


def _append_gist_performance(entry_md: str) -> None:
    """Hängt einen Eintrag an die Datei performance.md im State-Gist an."""
    try:
        headers = {"Authorization": f"token {GIST_TOKEN}"}
        r = _retry_request("GET", f"https://api.github.com/gists/{GIST_ID}", headers=headers)
        r.raise_for_status()
        files = r.json().get("files", {})
        old = (files.get("performance.md") or {}).get("content") or "# 📊 Performance-Log\n"
        _retry_request(
            "PATCH",
            f"https://api.github.com/gists/{GIST_ID}",
            headers=headers,
            json={"files": {"performance.md": {"content": old + entry_md}}},
        ).raise_for_status()
    except Exception as e:
        log.warning(f"Performance-Log (Gist) fehlgeschlagen: {e}")


def _write_performance_log(item: dict, stats: dict, age_days: float) -> None:
    emoji = {"instagram": "📸", "youtube": "▶️", "tiktok": "📱"}.get(item["platform"], "📊")
    kpis = _kpi_line(stats)
    notify(
        f"{emoji} **Performance-Log: {item['platform'].capitalize()}**\n"
        f"_{item['caption']}_\n"
        f"🎉 {_fmt_int(PERF_THRESHOLD_VIEWS)}+ Views nach {age_days:.1f} Tagen!\n{kpis}"
    )
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _append_gist_performance(
        f"\n---\n**{item['platform']}** · {date}\n"
        f"- Caption: {item['caption']}\n"
        f"- ID: `{item['id']}`\n"
        f"- Schwelle erreicht nach {age_days:.1f} Tagen\n"
        f"- {kpis}\n"
    )


def monitor_performance(state: dict, cookie_file: str, yt_token: str | None) -> None:
    """Beobachtet jeden Post 7 Tage; ab 2000 Views → Performance-Log (je Plattform unabhängig)."""
    watch = state.get("perf_watch", [])
    if not watch:
        return
    now = datetime.now(timezone.utc)
    changed = False
    remaining: list[dict] = []

    for item in watch:
        age_days = (now - datetime.fromisoformat(item["posted_at"])).total_seconds() / 86400
        if age_days > PERF_WATCH_DAYS:
            log.info(f"Perf-Watch beendet ({item['platform']} {item['id']}): 7 Tage um, unter Schwelle")
            changed = True
            continue

        last = item.get("last_checked")
        if last and (now - datetime.fromisoformat(last)).total_seconds() < PERF_CHECK_HOURS * 3600:
            remaining.append(item)
            continue
        item["last_checked"] = now.isoformat()
        changed = True

        platform = item["platform"]
        if platform == "instagram":
            stats = _ig_media_stats(item["id"], state["instagram_access_token"])
        elif platform == "youtube":
            stats = _yt_video_stats(item["id"], yt_token) if yt_token else None
        else:
            stats = _tiktok_video_stats(item.get("url", ""), cookie_file)

        if not stats:
            remaining.append(item)
            continue

        if stats.get("views", 0) >= PERF_THRESHOLD_VIEWS:
            log.info(f"🎉 Perf-Schwelle erreicht: {platform} {item['id']} ({stats.get('views')} Views)")
            _write_performance_log(item, stats, age_days)
        else:
            remaining.append(item)

    state["perf_watch"] = remaining
    if changed:
        write_state(state)


# ── YouTube A/B-Test ──────────────────────────────────────────────────────────
def _yt_update_title(video_id: str, new_title: str, token: str) -> bool:
    """Ändert den Titel eines Videos (snippet wird komplett übernommen, sonst löscht die API Felder)."""
    try:
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "snippet", "id": video_id},
            headers=headers, timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return False
        snippet = items[0]["snippet"]
        snippet["title"] = new_title

        r = requests.put(
            "https://www.googleapis.com/youtube/v3/videos?part=snippet",
            headers={**headers, "Content-Type": "application/json"},
            json={"id": video_id, "snippet": snippet},
            timeout=30,
        )
        if r.status_code >= 400:
            log.warning(f"YT Titel-Update fehlgeschlagen ({r.status_code}): {r.text[:300]}")
            if r.status_code == 403:
                notify(
                    "⚠️ **A/B-Test: keine Berechtigung zum Titel-Ändern.**\n"
                    "Einmal `setup_youtube.py` neu ausführen (erweiterte Scopes) und "
                    "YOUTUBE_REFRESH_TOKEN aktualisieren."
                )
            return False
        return True
    except Exception as e:
        log.warning(f"YT Titel-Update Fehler: {e}")
        return False


def process_youtube_ab(state: dict, yt_token: str | None) -> None:
    """24h Titel A → Views messen → 24h Titel B → Gewinner behalten."""
    ab_list = state.get("yt_ab", [])
    if not (ab_list and yt_token):
        return
    now = datetime.now(timezone.utc)
    changed = False
    remaining: list[dict] = []

    for ab in ab_list:
        phase = ab.get("phase", "a")
        age_h = (now - datetime.fromisoformat(ab["posted_at"])).total_seconds() / 3600

        # Sicherheitsnetz: hängende Tests nach 7 Tagen aufräumen
        if age_h > 7 * 24:
            log.info(f"A/B-Test {ab['video_id']} verwaist (>7 Tage) → entfernt")
            changed = True
            continue

        if phase == "a" and age_h >= AB_PHASE_HOURS:
            stats = _yt_video_stats(ab["video_id"], yt_token)
            if stats is None:
                remaining.append(ab)
                continue
            ab["views_a"] = stats["views"]
            if _yt_update_title(ab["video_id"], ab["title_b"], yt_token):
                ab["phase"] = "b"
                ab["switched_at"] = now.isoformat()
                notify(
                    f"🧪 **A/B-Test gestartet** für `{ab['video_id']}`\n"
                    f"A ({_fmt_int(stats['views'])} Views in 24h): {ab['title_a']}\n"
                    f"B (jetzt aktiv): {ab['title_b']}"
                )
                changed = True
                remaining.append(ab)
            else:
                # Titel-Wechsel fehlgeschlagen → bis zu 3x im nächsten Run erneut versuchen
                ab["update_fails"] = ab.get("update_fails", 0) + 1
                changed = True
                if ab["update_fails"] < 3:
                    remaining.append(ab)
                else:
                    log.warning(f"A/B-Test {ab['video_id']} abgebrochen (Titel-Update 3x fehlgeschlagen)")

        elif phase == "b" and ab.get("switched_at") and \
                (now - datetime.fromisoformat(ab["switched_at"])).total_seconds() / 3600 >= AB_PHASE_HOURS:
            stats = _yt_video_stats(ab["video_id"], yt_token)
            if stats is None:
                remaining.append(ab)
                continue
            views_a = ab.get("views_a", 0)
            views_b = max(0, stats["views"] - views_a)
            if views_a >= views_b:
                winner, winner_title = "A", ab["title_a"]
                _yt_update_title(ab["video_id"], ab["title_a"], yt_token)
            else:
                winner, winner_title = "B", ab["title_b"]
            notify(
                f"🏁 **A/B-Test beendet** `{ab['video_id']}`\n"
                f"A: {_fmt_int(views_a)} Views (0–24h) – {ab['title_a']}\n"
                f"B: {_fmt_int(views_b)} Views (24–48h) – {ab['title_b']}\n"
                f"🏆 Gewinner: **{winner}** → Titel: {winner_title}"
            )
            changed = True

        else:
            remaining.append(ab)

    state["yt_ab"] = remaining
    if changed:
        write_state(state)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=== Run gestartet ===")

    try:
        state = read_state()
    except Exception as e:
        msg = f"❌ Gist lesen fehlgeschlagen: {e}"
        log.error(msg)
        notify(msg)
        sys.exit(1)

    state["instagram_access_token"] = refresh_instagram_token(state)
    yt_token = get_youtube_token()
    last_id: str | None = state.get("last_video_id")

    with tempfile.TemporaryDirectory() as base_dir:
        cookie_file = write_cookie_file(base_dir)
        check_cookie_expiry(cookie_file, state)

        # Performance-Monitoring & laufende A/B-Tests (unabhängig von neuen Videos)
        monitor_performance(state, cookie_file, yt_token)
        process_youtube_ab(state, yt_token)

        try:
            videos = get_profile_videos(cookie_file)
        except Exception as e:
            msg = f"❌ TikTok Profil-Abruf fehlgeschlagen (Cookies abgelaufen?): {e}"
            log.error(msg)
            notify(msg)
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
            notify("✅ **Crossposter initialisiert!**\nAb dem nächsten TikTok-Video wird automatisch gepostet.")
            return

        new_videos = []
        for v in videos:
            if v["id"] == last_id:
                break
            new_videos.append(v)
        new_videos.reverse()  # älteste zuerst

        # ── Queue: neue Videos + Retries zusammenführen (crash-sicher im Gist) ──
        queue: list[dict] = state.setdefault("retry_queue", [])
        known_ids = {q["id"] for q in queue}
        for v in new_videos:
            if v["id"] not in known_ids:
                queue.append({"id": v["id"], "url": v["url"],
                              "description": v["description"], "attempts": 0})
        if new_videos:
            state["last_video_id"] = new_videos[-1]["id"]
        write_state(state)  # Heartbeat + Queue sofort persistieren

        if not queue:
            log.info(f"Keine neuen Videos seit {last_id}.")
            return

        if not in_posting_window():
            log.info(f"{len(queue)} Video(s) warten auf das Posting-Fenster ({POSTING_WINDOW} Uhr).")
            return

        log.info(f"{len(queue)} Video(s) in der Queue")
        posted_ids: list[str] = state.setdefault("posted_ids", [])

        for video in list(queue):
            video_id    = video["id"]
            raw_caption = video["description"]
            video_url   = video["url"]
            log.info(f"Verarbeite {video_id} (Versuch {video.get('attempts', 0) + 1}/{RETRY_MAX}): {raw_caption[:60]!r}")

            if video_id in posted_ids:
                log.info(f"Duplikat übersprungen: {video_id}")
                queue.remove(video)
                write_state(state)
                continue

            # Eigenes Verzeichnis pro Video → keine Konflikte
            work_dir = str(Path(base_dir) / video_id)
            Path(work_dir).mkdir(exist_ok=True)

            release_id: int | None = None
            tag = f"tmp-vid-{video_id}-{int(time.time())}"

            try:
                # ── Voll-Metadaten + Webseite holen (Privacy, echte Caption, Slideshow) ──
                log.info("Hole Video-Details...")
                info = _video_info(video_url, cookie_file)
                html = _fetch_tiktok_html(video_url, cookie_file)

                # ── Privatsphäre prüfen: private Videos NIE crossposten ──────────────
                privacy = tiktok_privacy(html, info)
                if privacy == "private":
                    private_since = video.get("private_since")
                    if private_since is None:
                        video["private_since"] = datetime.now(timezone.utc).isoformat()
                        log.info(f"🔒 {video_id} ist privat → übersprungen (wird {PRIVATE_RECHECK_DAYS} Tage erneut geprüft)")
                        notify(
                            f"🔒 **{video_id} ist privat** → wird nicht gepostet.\n"
                            f"Falls du es in den nächsten {PRIVATE_RECHECK_DAYS} Tagen auf "
                            f"öffentlich stellst, wird es automatisch nachgeholt."
                        )
                    else:
                        private_days = (datetime.now(timezone.utc)
                                        - datetime.fromisoformat(private_since)).total_seconds() / 86400
                        if private_days > PRIVATE_RECHECK_DAYS:
                            queue.remove(video)
                            log.info(f"🔒 {video_id} dauerhaft privat → endgültig übersprungen")
                            notify(f"🔒 **{video_id}** blieb privat → endgültig übersprungen.")
                        else:
                            log.info(f"🔒 {video_id} weiterhin privat ({private_days:.1f}/{PRIVATE_RECHECK_DAYS} Tage)")
                    write_state(state)
                    continue
                if privacy == "unknown":
                    log.warning("Privatsphäre-Status nicht ermittelbar → behandle als öffentlich")

                # ── Echte Caption aus Voll-Metadaten (Profil-Liste ist oft leer/falsch) ──
                full_caption = (info.get("description") or info.get("title") or "").strip()
                if full_caption and full_caption != raw_caption:
                    log.info(f"Caption korrigiert: {full_caption[:60]!r} (vorher: {raw_caption[:40]!r})")
                    raw_caption = full_caption
                    video["description"] = full_caption  # auch für Retries persistieren

                log.info("Lade Video herunter...")
                video_path = download_video(video_url, cookie_file, work_dir, info=info, html=html)
                log.info(f"Download OK: {video_path}")
                _log_video_specs(video_path)

                archive_video(video_path, video_id, raw_caption)

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
                yt_title_used = None
                if yt_token:
                    try:
                        yt_video_id, yt_title_used = upload_to_youtube(video_path, title_text, yt_desc, yt_token)
                        log.info(f"✅ YouTube Short: {yt_video_id}")
                    except Exception as e:
                        log.error(f"YouTube fehlgeschlagen: {e}", exc_info=True)
                        notify(f"⚠️ YouTube fehlgeschlagen für {video_id}: {e}")
                else:
                    log.warning("YouTube-Upload übersprungen: kein gültiges Token (siehe Fehler oben)")

                # Performance-Watch (je Plattform unabhängig) + A/B-Test registrieren
                now_iso   = datetime.now(timezone.utc).isoformat()
                cap_short = raw_caption[:80] or video_id
                perf = state.setdefault("perf_watch", [])
                perf.append({"platform": "tiktok", "id": video_id, "url": video_url,
                             "caption": cap_short, "posted_at": now_iso})
                perf.append({"platform": "instagram", "id": ig_id,
                             "caption": cap_short, "posted_at": now_iso})
                if yt_video_id:
                    perf.append({"platform": "youtube", "id": yt_video_id,
                                 "caption": cap_short, "posted_at": now_iso})
                    base = yt_title_used[:-8] if yt_title_used.endswith(" #Shorts") else yt_title_used
                    hook = AB_HOOKS[abs(hash(yt_video_id)) % len(AB_HOOKS)]
                    state.setdefault("yt_ab", []).append({
                        "video_id": yt_video_id,
                        "title_a": yt_title_used,
                        "title_b": f"{hook} {base}"[:91].rstrip() + " #Shorts",
                        "posted_at": now_iso,
                        "phase": "a",
                    })

                queue.remove(video)
                posted_ids.append(video_id)
                state["posted_ids"] = posted_ids[-100:]  # max 100 IDs behalten
                write_state(state)

                yt_line = f"\nYouTube: `{yt_video_id}`" if yt_video_id else ""
                notify(
                    f"✅ **Neuer Post!**\n"
                    f"Instagram: `{ig_id}`{yt_line}\n"
                    f"Caption: {raw_caption[:100]}"
                )

            except Exception as e:
                log.error(f"Fehler bei {video_id}: {e}", exc_info=True)
                video["attempts"] = video.get("attempts", 0) + 1
                if video["attempts"] >= RETRY_MAX:
                    queue.remove(video)
                    notify(
                        f"❌ **{video_id} endgültig fehlgeschlagen** "
                        f"(nach {RETRY_MAX} Versuchen aufgegeben)\n{type(e).__name__}: {e}"
                    )
                else:
                    notify(
                        f"⚠️ **Fehler bei {video_id}** "
                        f"(Versuch {video['attempts']}/{RETRY_MAX}, wird beim nächsten Run erneut versucht)\n"
                        f"{type(e).__name__}: {e}"
                    )
                write_state(state)
            finally:
                if release_id is not None:
                    delete_github_release(release_id, tag)
                # Work-Dir aufräumen
                shutil.rmtree(work_dir, ignore_errors=True)

    log.info("=== Run beendet ===")


if __name__ == "__main__":
    main()
