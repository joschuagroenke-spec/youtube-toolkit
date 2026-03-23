from __future__ import annotations

import json
import os
import re
import shutil
import threading
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse, urlsplit
from urllib.request import Request, urlopen

import yt_dlp


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
COOKIE_BROWSERS = (None, "chrome", "edge", "firefox", "brave")
VIDEO_QUALITY_FORMATS = {
    "best": "bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best",
    "720p": "bestvideo[height<=720]+bestaudio/best",
    "480p": "bestvideo[height<=480]+bestaudio/best",
    "360p": "bestvideo[height<=360]+bestaudio/bestaudio/best",
}
VIDEO_QUALITY_HEIGHT = {
    "best": None,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}

_download_dir_lock = threading.Lock()
_custom_download_dir: Path | None = None


class QuietLogger:
    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


@dataclass
class BackendError(Exception):
    code: str
    message: str
    http_status: int = 400

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error_code": self.code, "message": self.message}


def get_downloads_dir() -> Path:
    downloads_dir = Path.home() / "Downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    return downloads_dir


def get_current_download_dir() -> Path:
    with _download_dir_lock:
        current = _custom_download_dir
    target = current if current is not None else get_downloads_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


def set_current_download_dir(path_value: str) -> Path:
    path_text = (path_value or "").strip()
    if not path_text:
        raise BackendError(
            code="INVALID_FOLDER",
            message="Ungueltiger Ordnerpfad.",
            http_status=400,
        )

    target = Path(path_text).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    if not target.exists() or not target.is_dir():
        raise BackendError(
            code="INVALID_FOLDER",
            message="Der angegebene Pfad ist kein gueltiger Ordner.",
            http_status=400,
        )

    with _download_dir_lock:
        global _custom_download_dir
        _custom_download_dir = target
    return target


def select_download_dir_via_dialog() -> Path:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise BackendError(
            code="FOLDER_DIALOG_UNAVAILABLE",
            message=f"Ordner-Auswahldialog nicht verfuegbar. ({exc})",
            http_status=500,
        ) from exc

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    selected = filedialog.askdirectory(title="Download-Ordner waehlen")
    root.destroy()

    if not selected:
        raise BackendError(
            code="FOLDER_NOT_SELECTED",
            message="Kein Ordner ausgewaehlt.",
            http_status=400,
        )

    return set_current_download_dir(selected)


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip())
    return (cleaned[:150] or "youtube_download").strip()


def extract_video_id(url: str) -> str | None:
    if not url or not isinstance(url, str):
        return None

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    video_id = None

    if host == "youtu.be":
        if path_parts:
            video_id = path_parts[0]
    elif host in YOUTUBE_HOSTS:
        if parsed.path == "/watch":
            params = parse_qs(parsed.query)
            values = params.get("v") or []
            video_id = values[0] if values else None
        elif len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed"}:
            video_id = path_parts[1]

    if not video_id:
        return None

    if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return video_id
    return None


def is_valid_youtube_url(url: str) -> bool:
    return extract_video_id(url) is not None


def ensure_valid_youtube_url(url: str) -> str:
    trimmed = (url or "").strip()
    if not is_valid_youtube_url(trimmed):
        raise BackendError(
            code="INVALID_URL",
            message="Ungueltige YouTube-URL. Bitte einen gueltigen Link eingeben.",
            http_status=400,
        )
    return trimmed


def normalize_video_quality(quality: str | None) -> str:
    requested = (quality or "").strip().lower()
    if requested in VIDEO_QUALITY_FORMATS:
        return requested
    return "1080p"


def find_ffmpeg_path() -> str | None:
    ffmpeg_in_path = shutil.which("ffmpeg")
    if ffmpeg_in_path:
        return ffmpeg_in_path

    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if not local_appdata:
        return None

    winget_packages = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
    if not winget_packages.exists():
        return None

    candidates = list(winget_packages.glob("yt-dlp.FFmpeg_*/*/bin/ffmpeg.exe"))
    if candidates:
        newest = max(candidates, key=lambda path: path.stat().st_mtime)
        return str(newest)

    return None


def build_common_ydl_opts() -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "logger": QuietLogger(),
    }
    ffmpeg_path = find_ffmpeg_path()
    if ffmpeg_path:
        options["ffmpeg_location"] = ffmpeg_path
    return options


def pick_thumbnail(info: dict[str, Any]) -> str | None:
    direct_thumbnail = info.get("thumbnail")
    if direct_thumbnail:
        return str(direct_thumbnail)

    thumbnails = info.get("thumbnails") or []
    if not thumbnails:
        return None

    def sort_key(item: dict[str, Any]) -> tuple[int, int]:
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        return width * height, height

    best = max(thumbnails, key=sort_key)
    return str(best.get("url")) if best.get("url") else None


def _clean_transcript_text(value: str) -> str:
    cleaned = unescape(value or "")
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = cleaned.replace("\n", " ").strip()
    return cleaned


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _parse_vtt_timestamp(value: str) -> float | None:
    text = (value or "").strip().replace(",", ".")
    if not text:
        return None

    parts = text.split(":")
    try:
        if len(parts) == 3:
            hours = float(parts[0])
            minutes = float(parts[1])
            seconds = float(parts[2])
            return (hours * 3600.0) + (minutes * 60.0) + seconds
        if len(parts) == 2:
            minutes = float(parts[0])
            seconds = float(parts[1])
            return (minutes * 60.0) + seconds
    except Exception:
        return None
    return None


def _format_timestamp(seconds: float | int | None) -> str:
    total = max(0, int(float(seconds or 0)))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_json3_entries(raw_text: str) -> list[dict[str, Any]]:
    data = json.loads(raw_text)
    entries: list[dict[str, Any]] = []

    for event in data.get("events", []):
        segs = event.get("segs") or []
        parts: list[str] = []
        for seg in segs:
            value = seg.get("utf8", "")
            if value:
                parts.append(value)

        line = _clean_transcript_text("".join(parts))
        if not line:
            continue

        start_ms = _as_float(event.get("tStartMs"))
        start_seconds = (start_ms / 1000.0) if start_ms is not None else 0.0
        entries.append({"start_seconds": max(0.0, start_seconds), "text": line})

    return entries


def parse_vtt_entries(raw_text: str) -> list[dict[str, Any]]:
    lines = raw_text.replace("\r", "").split("\n")
    entries: list[dict[str, Any]] = []
    idx = 0

    while idx < len(lines):
        line = lines[idx].strip()
        if not line or line.startswith("WEBVTT") or line.startswith("NOTE"):
            idx += 1
            continue

        if "-->" not in line:
            # Cue IDs are allowed before the time range line.
            if idx + 1 < len(lines) and "-->" in lines[idx + 1]:
                idx += 1
                line = lines[idx].strip()
            else:
                idx += 1
                continue

        if "-->" not in line:
            idx += 1
            continue

        start_token = line.split("-->", 1)[0].strip().split(" ")[0]
        start_seconds = _parse_vtt_timestamp(start_token)

        idx += 1
        text_lines: list[str] = []
        while idx < len(lines):
            candidate = lines[idx].strip()
            if not candidate:
                break
            if "-->" in candidate:
                break
            if candidate.startswith("Kind:") or candidate.startswith("Language:"):
                idx += 1
                continue
            text_lines.append(candidate)
            idx += 1

        if start_seconds is None:
            start_seconds = 0.0

        line_text = _clean_transcript_text(" ".join(text_lines))
        if line_text:
            entries.append({"start_seconds": max(0.0, start_seconds), "text": line_text})

        idx += 1

    return entries


def build_plain_transcript(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for entry in entries:
        text = _clean_transcript_text(str(entry.get("text") or ""))
        if text:
            lines.append(text)

    deduped: list[str] = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return "\n".join(deduped).strip()


def build_timestamp_transcript(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for entry in entries:
        text = _clean_transcript_text(str(entry.get("text") or ""))
        if not text:
            continue
        stamp = _format_timestamp(entry.get("start_seconds"))
        lines.append(f"[{stamp}] {text}")

    deduped: list[str] = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return "\n".join(deduped).strip()


def choose_subtitle_entry(info: dict[str, Any]) -> dict[str, str] | None:
    candidates: list[tuple[int, int, int, str, str, str]] = []
    language_order = ("de", "en")
    ext_order = ("json3", "vtt")

    for source_rank, source_key in enumerate(("subtitles", "automatic_captions")):
        subtitle_map = info.get(source_key) or {}
        for language, entries in subtitle_map.items():
            for entry in entries:
                url = entry.get("url")
                extension = (entry.get("ext") or "").lower()
                if not url:
                    continue

                lang_rank = len(language_order)
                normalized_lang = (language or "").lower()
                for idx, prefix in enumerate(language_order):
                    if normalized_lang.startswith(prefix):
                        lang_rank = idx
                        break

                ext_rank = ext_order.index(extension) if extension in ext_order else len(ext_order)
                candidates.append((source_rank, lang_rank, ext_rank, language, extension, url))

    if not candidates:
        return None

    _, _, _, language, extension, url = min(candidates, key=lambda item: (item[0], item[1], item[2]))
    return {"lang": language, "ext": extension, "url": url}


def extract_chapters(info: dict[str, Any]) -> list[dict[str, Any]]:
    raw_chapters = info.get("chapters") or []
    chapters: list[dict[str, Any]] = []
    for chapter in raw_chapters:
        start_time = _as_float(chapter.get("start_time"))
        end_time = _as_float(chapter.get("end_time"))
        title = str(chapter.get("title") or "").strip() or "Kapitel"
        start_seconds = max(0.0, start_time or 0.0)
        chapters.append(
            {
                "title": title,
                "start_seconds": start_seconds,
                "end_seconds": max(start_seconds, end_time) if end_time is not None else None,
                "time_label": _format_timestamp(start_seconds),
            }
        )
    return chapters


def map_exception(exc: Exception, *, default_code: str, default_message: str, default_status: int) -> BackendError:
    message = str(exc).strip()
    lower = message.lower()

    if isinstance(exc, BackendError):
        return exc
    if "429" in message:
        return BackendError(
            code="YOUTUBE_RATE_LIMIT_429",
            message="YouTube blockiert die Anfrage (HTTP 429). Bitte spaeter erneut versuchen.",
            http_status=429,
        )
    if "ffmpeg" in lower and ("not installed" in lower or "not found" in lower):
        return BackendError(
            code="FFMPEG_MISSING",
            message="ffmpeg wurde nicht gefunden. MP3-Download benoetigt ffmpeg.",
            http_status=400,
        )
    if "unsupported url" in lower or "invalid url" in lower:
        return BackendError(
            code="INVALID_URL",
            message="Ungueltige YouTube-URL. Bitte einen gueltigen Link eingeben.",
            http_status=400,
        )

    short = message.splitlines()[0] if message else default_message
    return BackendError(code=default_code, message=f"{default_message} ({short})", http_status=default_status)


def fetch_info(video_url: str, *, cookies_browser: str | None = None) -> dict[str, Any]:
    options = build_common_ydl_opts()
    options["skip_download"] = True
    if cookies_browser:
        options["cookiesfrombrowser"] = (cookies_browser,)

    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(video_url, download=False)


def fetch_transcript(
    video_url: str,
    *,
    allow_missing: bool = False,
    include_timestamps: bool = False,
) -> dict[str, Any]:
    url = ensure_valid_youtube_url(video_url)
    errors: list[str] = []

    for browser in COOKIE_BROWSERS:
        options = build_common_ydl_opts()
        options["skip_download"] = True
        if browser:
            options["cookiesfrombrowser"] = (browser,)

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
                subtitle = choose_subtitle_entry(info)

                if not subtitle:
                    if allow_missing:
                        return {
                            "transcript": "",
                            "transcript_plain": "",
                            "transcript_with_timestamps": "",
                            "transcript_available": False,
                            "language": None,
                            "title": str(info.get("title") or "YouTube Video"),
                            "video_id": str(info.get("id") or ""),
                            "include_timestamps": include_timestamps,
                        }
                    raise BackendError(
                        code="NO_SUBTITLES",
                        message="Keine Untertitel fuer dieses Video gefunden.",
                        http_status=404,
                    )

                response = ydl.urlopen(subtitle["url"])
                raw_data = response.read().decode("utf-8", errors="replace")

                entries: list[dict[str, Any]] = []
                if subtitle["ext"] == "json3":
                    try:
                        entries = parse_json3_entries(raw_data)
                    except json.JSONDecodeError:
                        entries = []
                elif subtitle["ext"] == "vtt":
                    entries = parse_vtt_entries(raw_data)
                else:
                    entries = parse_vtt_entries(raw_data)
                    if not entries:
                        try:
                            entries = parse_json3_entries(raw_data)
                        except json.JSONDecodeError:
                            entries = []

                plain_text = build_plain_transcript(entries)
                timestamp_text = build_timestamp_transcript(entries)
                selected_text = timestamp_text if include_timestamps else plain_text

                if not selected_text:
                    if allow_missing:
                        return {
                            "transcript": "",
                            "transcript_plain": plain_text,
                            "transcript_with_timestamps": timestamp_text,
                            "transcript_available": False,
                            "language": subtitle["lang"],
                            "title": str(info.get("title") or "YouTube Video"),
                            "video_id": str(info.get("id") or ""),
                            "include_timestamps": include_timestamps,
                        }
                    raise BackendError(
                        code="TRANSCRIPT_PARSE_FAILED",
                        message="Transkript konnte nicht aus den Untertiteln gelesen werden.",
                        http_status=422,
                    )

                return {
                    "transcript": selected_text,
                    "transcript_plain": plain_text,
                    "transcript_with_timestamps": timestamp_text,
                    "transcript_available": True,
                    "language": subtitle["lang"],
                    "title": str(info.get("title") or "YouTube Video"),
                    "video_id": str(info.get("id") or ""),
                    "include_timestamps": include_timestamps,
                }
        except BackendError:
            raise
        except Exception as exc:
            errors.append(str(exc))
            continue

    if any("429" in err for err in errors):
        raise BackendError(
            code="YOUTUBE_RATE_LIMIT_429",
            message="YouTube blockiert die Untertitel-Anfrage (HTTP 429). Bitte spaeter erneut versuchen.",
            http_status=429,
        )

    if errors:
        detail = errors[-1].splitlines()[0]
        raise BackendError(
            code="DOWNLOAD_FAILED",
            message=f"Untertitel konnten nicht geladen werden. ({detail})",
            http_status=502,
        )

    raise BackendError(
        code="DOWNLOAD_FAILED",
        message="Untertitel konnten nicht geladen werden.",
        http_status=502,
    )


def fetch_video_preview(video_url: str, *, include_timestamps: bool = False) -> dict[str, Any]:
    url = ensure_valid_youtube_url(video_url)
    try:
        info = fetch_info(url)
    except Exception as exc:
        raise map_exception(
            exc,
            default_code="DOWNLOAD_FAILED",
            default_message="Videovorschau konnte nicht geladen werden.",
            default_status=502,
        )

    transcript = ""
    transcript_available = False
    language = None
    transcript_error_code = None
    transcript_error_message = None
    chapters = extract_chapters(info)

    try:
        transcript_info = fetch_transcript(url, allow_missing=True, include_timestamps=include_timestamps)
        transcript = str(transcript_info.get("transcript") or "")
        transcript_available = bool(transcript_info.get("transcript_available"))
        language = transcript_info.get("language")
    except BackendError as exc:
        transcript_error_code = exc.code
        transcript_error_message = exc.message

    return {
        "title": str(info.get("title") or "YouTube Video"),
        "thumbnail_url": pick_thumbnail(info),
        "video_id": str(info.get("id") or ""),
        "transcript": transcript,
        "transcript_available": transcript_available,
        "language": language,
        "include_timestamps": include_timestamps,
        "chapters": chapters,
        "transcript_error_code": transcript_error_code,
        "transcript_error_message": transcript_error_message,
    }


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _build_progress_hook(progress_callback: Callable[[dict[str, Any]], None] | None):
    if progress_callback is None:
        return None

    def hook(progress_data: dict[str, Any]) -> None:
        payload = {
            "status": progress_data.get("status"),
            "downloaded_bytes": _safe_int(progress_data.get("downloaded_bytes")),
            "total_bytes": _safe_int(progress_data.get("total_bytes")),
            "total_bytes_estimate": _safe_int(progress_data.get("total_bytes_estimate")),
            "speed": _safe_float(progress_data.get("speed")),
            "eta": _safe_int(progress_data.get("eta")),
            "filename": progress_data.get("filename"),
        }
        progress_callback(payload)

    return hook


def _build_single_file_video_format(quality: str) -> str:
    height = VIDEO_QUALITY_HEIGHT.get(quality)
    if height is None:
        return "best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best"

    return (
        f"best[ext=mp4][height<={height}][vcodec!=none][acodec!=none]/"
        f"best[height<={height}][vcodec!=none][acodec!=none]/"
        "best[vcodec!=none][acodec!=none]/best"
    )


def _resolve_saved_path(info: dict[str, Any], target_dir: Path, preferred_ext: str | None = None) -> Path | None:
    candidate_paths: list[Path] = []

    for key in ("filepath", "_filename"):
        value = info.get(key)
        if isinstance(value, str) and value:
            candidate_paths.append(Path(value))

    for item in info.get("requested_downloads") or []:
        value = item.get("filepath")
        if isinstance(value, str) and value:
            candidate_paths.append(Path(value))

    files_to_move = info.get("__files_to_move") or {}
    if isinstance(files_to_move, dict):
        for target in files_to_move.values():
            if isinstance(target, str) and target:
                candidate_paths.append(Path(target))

    for path in candidate_paths:
        if path.exists() and path.is_file():
            if preferred_ext is None or path.suffix.lower() == f".{preferred_ext.lower()}":
                return path

    video_id = str(info.get("id") or "")
    if not video_id:
        return None

    tagged = [path for path in target_dir.iterdir() if path.is_file() and f"[{video_id}]" in path.name]
    if preferred_ext:
        preferred = [path for path in tagged if path.suffix.lower() == f".{preferred_ext.lower()}"]
        if preferred:
            return max(preferred, key=lambda path: path.stat().st_mtime)

    if tagged:
        return max(tagged, key=lambda path: path.stat().st_mtime)

    return None


def download_video_mp4(
    video_url: str,
    *,
    quality: str = "1080p",
    download_dir: Path | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    url = ensure_valid_youtube_url(video_url)
    selected_quality = normalize_video_quality(quality)
    target_dir = (download_dir or get_current_download_dir()).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_path = find_ffmpeg_path()
    options = build_common_ydl_opts()
    options.update(
        {
            "outtmpl": str(target_dir / "%(title).150B [%(id)s].%(ext)s"),
            "noplaylist": True,
        }
    )

    hook = _build_progress_hook(progress_callback)
    if hook is not None:
        options["progress_hooks"] = [hook]

    if ffmpeg_path:
        options["format"] = VIDEO_QUALITY_FORMATS[selected_quality]
        options["merge_output_format"] = "mp4"
    else:
        options["format"] = _build_single_file_video_format(selected_quality)

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        raise map_exception(
            exc,
            default_code="DOWNLOAD_FAILED",
            default_message="Video-Download fehlgeschlagen.",
            default_status=502,
        )

    saved_path = _resolve_saved_path(info, target_dir, preferred_ext="mp4")
    if saved_path is None:
        saved_path = _resolve_saved_path(info, target_dir)
    if saved_path is None:
        raise BackendError(
            code="DOWNLOAD_FAILED",
            message="Download abgeschlossen, aber Ausgabedatei konnte nicht gefunden werden.",
            http_status=500,
        )

    if progress_callback is not None:
        progress_callback({"status": "completed", "saved_to": str(saved_path), "quality": selected_quality})

    return {"saved_to": str(saved_path), "quality": selected_quality}


def download_audio_mp3(
    video_url: str,
    *,
    download_dir: Path | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    url = ensure_valid_youtube_url(video_url)
    target_dir = (download_dir or get_current_download_dir()).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_path = find_ffmpeg_path()
    if not ffmpeg_path:
        raise BackendError(
            code="FFMPEG_MISSING",
            message="ffmpeg wurde nicht gefunden. MP3-Download benoetigt ffmpeg.",
            http_status=400,
        )

    options = build_common_ydl_opts()
    options.update(
        {
            "format": "bestaudio/best",
            "outtmpl": str(target_dir / "%(title).150B [%(id)s].%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
            "noplaylist": True,
        }
    )

    hook = _build_progress_hook(progress_callback)
    if hook is not None:
        options["progress_hooks"] = [hook]

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        raise map_exception(
            exc,
            default_code="DOWNLOAD_FAILED",
            default_message="Audio-Download fehlgeschlagen.",
            default_status=502,
        )

    saved_path = _resolve_saved_path(info, target_dir, preferred_ext="mp3")
    if saved_path is None:
        saved_path = _resolve_saved_path(info, target_dir)
    if saved_path is None:
        raise BackendError(
            code="DOWNLOAD_FAILED",
            message="Download abgeschlossen, aber MP3-Datei konnte nicht gefunden werden.",
            http_status=500,
        )

    if progress_callback is not None:
        progress_callback({"status": "completed", "saved_to": str(saved_path)})

    return {"saved_to": str(saved_path)}


def download_transcript_txt(
    video_url: str,
    *,
    include_timestamps: bool = False,
    download_dir: Path | None = None,
) -> dict[str, Any]:
    url = ensure_valid_youtube_url(video_url)
    target_dir = (download_dir or get_current_download_dir()).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        transcript_info = fetch_transcript(
            url,
            allow_missing=False,
            include_timestamps=include_timestamps,
        )
    except Exception as exc:
        raise map_exception(
            exc,
            default_code="DOWNLOAD_FAILED",
            default_message="Transcript-Download fehlgeschlagen.",
            default_status=502,
        )

    title = sanitize_filename(str(transcript_info.get("title") or "transcript"))
    video_id = str(transcript_info.get("video_id") or "")
    suffix = f" [{video_id}]" if video_id else ""
    ts_suffix = " - timestamps" if include_timestamps else ""
    transcript_path = target_dir / f"{title}{suffix} - transcript{ts_suffix}.txt"
    transcript_path.write_text(str(transcript_info.get("transcript") or ""), encoding="utf-8")

    return {
        "saved_to": str(transcript_path),
        "language": transcript_info.get("language"),
        "include_timestamps": include_timestamps,
    }


def download_thumbnail(video_url: str, *, download_dir: Path | None = None) -> dict[str, Any]:
    url = ensure_valid_youtube_url(video_url)
    target_dir = (download_dir or get_current_download_dir()).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        info = fetch_info(url)
    except Exception as exc:
        raise map_exception(
            exc,
            default_code="DOWNLOAD_FAILED",
            default_message="Thumbnail konnte nicht geladen werden.",
            default_status=502,
        )

    thumbnail_url = pick_thumbnail(info)
    if not thumbnail_url:
        raise BackendError(
            code="THUMBNAIL_MISSING",
            message="Kein Thumbnail fuer dieses Video gefunden.",
            http_status=404,
        )

    video_id = str(info.get("id") or "")
    title = sanitize_filename(str(info.get("title") or "thumbnail"))
    suffix = f" [{video_id}]" if video_id else ""

    ext = Path(urlsplit(thumbnail_url).path).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".jpg"

    file_path = target_dir / f"{title}{suffix} - thumbnail{ext}"

    try:
        request = Request(thumbnail_url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=30) as response:
            image_data = response.read()
    except Exception as exc:
        raise map_exception(
            exc,
            default_code="DOWNLOAD_FAILED",
            default_message="Thumbnail-Download fehlgeschlagen.",
            default_status=502,
        )

    file_path.write_bytes(image_data)
    return {"saved_to": str(file_path), "thumbnail_url": thumbnail_url}
