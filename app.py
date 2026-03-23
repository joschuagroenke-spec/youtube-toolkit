from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from flask import Flask, after_this_request, jsonify, request, send_file, send_from_directory

from backend import (
    BackendError,
    download_audio_mp3,
    download_thumbnail,
    download_transcript_txt,
    download_video_mp4,
    fetch_video_preview,
    get_current_download_dir,
    map_exception,
    normalize_video_quality,
    select_download_dir_via_dialog,
    set_current_download_dir,
)


BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = "youtube_downloader.html"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5000

app = Flask(__name__)

_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_files_lock = threading.Lock()
_files: dict[str, dict[str, Any]] = {}


def _delete_file_later(path: Path) -> None:
    def worker():
        for _ in range(8):
            try:
                if path.exists():
                    path.unlink()
                break
            except Exception:
                time.sleep(0.5)

    threading.Thread(target=worker, daemon=True).start()


def _get_local_network_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def _get_server_port() -> int:
    raw_value = (os.environ.get("PORT") or "").strip()
    if not raw_value:
        return DEFAULT_PORT

    try:
        port = int(raw_value)
    except ValueError:
        return DEFAULT_PORT

    return port if 1 <= port <= 65535 else DEFAULT_PORT


def _get_server_host() -> str:
    value = (os.environ.get("HOST") or "").strip()
    return value or DEFAULT_HOST


def _error_response(exc: Exception):
    if isinstance(exc, BackendError):
        return jsonify(exc.to_dict()), exc.http_status
    mapped = map_exception(
        exc,
        default_code="DOWNLOAD_FAILED",
        default_message="Interner Serverfehler.",
        default_status=500,
    )
    return jsonify(mapped.to_dict()), mapped.http_status


def _get_url_from_request() -> str:
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        raise BackendError(
            code="INVALID_URL",
            message="Bitte eine YouTube-URL eingeben.",
            http_status=400,
        )
    return url


def _is_local_request() -> bool:
    remote_addr = (request.remote_addr or "").strip()
    return remote_addr in {"127.0.0.1", "::1", "localhost"}


def _register_download_file(path_value: str | None, *, delete_after_download: bool = False) -> dict[str, str] | None:
    if not path_value:
        return None

    path = Path(path_value)
    if not path.exists() or not path.is_file():
        return None

    file_id = str(uuid.uuid4())
    with _files_lock:
        _files[file_id] = {"path": path, "delete_after_download": delete_after_download}

    return {
        "file_id": file_id,
        "download_url": f"/download/file/{file_id}",
        "file_name": path.name,
    }


def _create_job(
    job_type: str,
    url: str,
    quality: str | None = None,
    *,
    remote_client: bool = False,
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    now = time.time()
    job = {
        "job_id": job_id,
        "type": job_type,
        "url": url,
        "quality": quality,
        "status": "queued",
        "message": "Waiting...",
        "progress_percent": 0.0,
        "downloaded_bytes": 0,
        "total_bytes": None,
        "speed": None,
        "eta": None,
        "saved_to": None,
        "file_id": None,
        "download_url": None,
        "file_name": None,
        "remote_client": remote_client,
        "error_code": None,
        "created_at": now,
        "updated_at": now,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    return dict(job)


def _update_job(job_id: str, **fields: Any) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = time.time()


def _get_job(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _progress_callback_for(job_id: str, media_label: str):
    def callback(payload: dict[str, Any]) -> None:
        status = payload.get("status")
        downloaded_bytes = payload.get("downloaded_bytes")
        total_bytes = payload.get("total_bytes") or payload.get("total_bytes_estimate")
        speed = payload.get("speed")
        eta = payload.get("eta")
        progress_percent = 0.0

        if isinstance(downloaded_bytes, int) and isinstance(total_bytes, int) and total_bytes > 0:
            progress_percent = min(100.0, max(0.0, (downloaded_bytes / total_bytes) * 100.0))

        if status == "downloading":
            _update_job(
                job_id,
                status="downloading",
                message=f"Downloading {media_label}...",
                downloaded_bytes=downloaded_bytes or 0,
                total_bytes=total_bytes,
                speed=speed,
                eta=eta,
                progress_percent=progress_percent,
            )
        elif status == "finished":
            _update_job(
                job_id,
                status="processing",
                message="Download completed. Finalizing file...",
                progress_percent=max(progress_percent, 99.0),
                downloaded_bytes=downloaded_bytes or 0,
                total_bytes=total_bytes,
                speed=speed,
                eta=eta,
            )
        elif status == "completed":
            _update_job(
                job_id,
                status="finished",
                message="Download finished.",
                progress_percent=100.0,
                saved_to=payload.get("saved_to"),
                speed=speed,
                eta=0,
            )

    return callback


def _run_video_job(job_id: str, url: str, quality: str, download_dir: Path, remote_client: bool) -> None:
    _update_job(job_id, status="downloading", message="Starting video download...")
    callback = _progress_callback_for(job_id, "video")
    try:
        result = download_video_mp4(
            url,
            quality=quality,
            download_dir=download_dir,
            progress_callback=callback,
        )
        file_meta = _register_download_file(
            result.get("saved_to"),
            delete_after_download=remote_client,
        )
        _update_job(
            job_id,
            status="finished",
            message="Download finished.",
            progress_percent=100.0,
            saved_to=result.get("saved_to"),
            quality=result.get("quality"),
            file_id=(file_meta or {}).get("file_id"),
            download_url=(file_meta or {}).get("download_url"),
            file_name=(file_meta or {}).get("file_name"),
            eta=0,
        )
    except Exception as exc:
        mapped = exc if isinstance(exc, BackendError) else map_exception(
            exc,
            default_code="DOWNLOAD_FAILED",
            default_message="Video-Download fehlgeschlagen.",
            default_status=502,
        )
        _update_job(
            job_id,
            status="error",
            message=mapped.message,
            error_code=mapped.code,
        )


def _run_audio_job(job_id: str, url: str, download_dir: Path, remote_client: bool) -> None:
    _update_job(job_id, status="downloading", message="Starting audio download...")
    callback = _progress_callback_for(job_id, "audio")
    try:
        result = download_audio_mp3(
            url,
            download_dir=download_dir,
            progress_callback=callback,
        )
        file_meta = _register_download_file(
            result.get("saved_to"),
            delete_after_download=remote_client,
        )
        _update_job(
            job_id,
            status="finished",
            message="Download finished.",
            progress_percent=100.0,
            saved_to=result.get("saved_to"),
            file_id=(file_meta or {}).get("file_id"),
            download_url=(file_meta or {}).get("download_url"),
            file_name=(file_meta or {}).get("file_name"),
            eta=0,
        )
    except Exception as exc:
        mapped = exc if isinstance(exc, BackendError) else map_exception(
            exc,
            default_code="DOWNLOAD_FAILED",
            default_message="Audio-Download fehlgeschlagen.",
            default_status=502,
        )
        _update_job(
            job_id,
            status="error",
            message=mapped.message,
            error_code=mapped.code,
        )


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, HTML_FILE)


@app.post("/video-info")
def video_info():
    try:
        data = request.get_json(silent=True) or {}
        url = _get_url_from_request()
        include_timestamps = bool(data.get("include_timestamps", False))
        preview = fetch_video_preview(url, include_timestamps=include_timestamps)
        return jsonify({"ok": True, **preview})
    except Exception as exc:
        return _error_response(exc)


@app.post("/download/video")
def download_video():
    try:
        data = request.get_json(silent=True) or {}
        url = _get_url_from_request()
        quality = normalize_video_quality(data.get("quality"))
        folder = get_current_download_dir()
        remote_client = not _is_local_request()
        job = _create_job("video", url, quality=quality, remote_client=remote_client)
        thread = threading.Thread(
            target=_run_video_job,
            args=(job["job_id"], url, quality, folder, remote_client),
            daemon=True,
        )
        thread.start()
        return jsonify(
            {
                "ok": True,
                "job_id": job["job_id"],
                "quality": quality,
                "download_folder": str(folder),
            }
        )
    except Exception as exc:
        return _error_response(exc)


@app.post("/download/audio")
def download_audio():
    try:
        url = _get_url_from_request()
        folder = get_current_download_dir()
        remote_client = not _is_local_request()
        job = _create_job("audio", url, remote_client=remote_client)
        thread = threading.Thread(
            target=_run_audio_job,
            args=(job["job_id"], url, folder, remote_client),
            daemon=True,
        )
        thread.start()
        return jsonify(
            {
                "ok": True,
                "job_id": job["job_id"],
                "download_folder": str(folder),
            }
        )
    except Exception as exc:
        return _error_response(exc)


@app.get("/download/progress")
def download_progress():
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify(
            {
                "ok": False,
                "error_code": "MISSING_JOB_ID",
                "message": "job_id fehlt.",
            }
        ), 400

    job = _get_job(job_id)
    if not job:
        return jsonify(
            {
                "ok": False,
                "error_code": "JOB_NOT_FOUND",
                "message": "Download-Job nicht gefunden.",
            }
        ), 404

    return jsonify({"ok": True, **job})


@app.post("/download/transcript")
def download_transcript():
    try:
        data = request.get_json(silent=True) or {}
        url = _get_url_from_request()
        include_timestamps = bool(data.get("include_timestamps", False))
        result = download_transcript_txt(
            url,
            include_timestamps=include_timestamps,
            download_dir=get_current_download_dir(),
        )
        file_meta = _register_download_file(
            result.get("saved_to"),
            delete_after_download=not _is_local_request(),
        )
        return jsonify({"ok": True, **result, **(file_meta or {}), "download_folder": str(get_current_download_dir())})
    except Exception as exc:
        return _error_response(exc)


@app.post("/download/thumbnail")
def download_thumbnail_file():
    try:
        url = _get_url_from_request()
        result = download_thumbnail(url, download_dir=get_current_download_dir())
        file_meta = _register_download_file(
            result.get("saved_to"),
            delete_after_download=not _is_local_request(),
        )
        return jsonify({"ok": True, **result, **(file_meta or {}), "download_folder": str(get_current_download_dir())})
    except Exception as exc:
        return _error_response(exc)


@app.get("/download/file/<file_id>")
def download_file(file_id: str):
    with _files_lock:
        entry = _files.get(file_id)
    if not entry:
        return jsonify({"ok": False, "error_code": "FILE_NOT_FOUND", "message": "Datei nicht gefunden."}), 404

    file_path = entry.get("path")
    delete_after_download = bool(entry.get("delete_after_download"))
    if not file_path or not file_path.exists() or not file_path.is_file():
        return jsonify({"ok": False, "error_code": "FILE_NOT_FOUND", "message": "Datei nicht gefunden."}), 404

    @after_this_request
    def cleanup_file(response):
        if delete_after_download:
            _delete_file_later(file_path)
        with _files_lock:
            _files.pop(file_id, None)
        return response

    return send_file(file_path, as_attachment=True, download_name=file_path.name)


@app.post("/set-download-folder")
def set_download_folder():
    try:
        data = request.get_json(silent=True) or {}
        selected_path = (data.get("path") or "").strip()
        use_dialog = bool(data.get("use_dialog", not selected_path))

        if use_dialog:
            folder = select_download_dir_via_dialog()
        else:
            folder = set_current_download_dir(selected_path)

        return jsonify({"ok": True, "download_folder": str(folder)})
    except Exception as exc:
        return _error_response(exc)


@app.get("/download-folder")
def get_download_folder():
    folder = get_current_download_dir()
    return jsonify({"ok": True, "download_folder": str(folder)})


# Compatibility aliases for existing frontend calls.
@app.post("/api/preview")
def api_preview():
    return video_info()


@app.post("/api/download/video")
def api_download_video():
    return download_video()


@app.post("/api/download/audio")
def api_download_audio():
    return download_audio()


@app.get("/api/download/progress")
def api_download_progress():
    return download_progress()


@app.get("/api/download/file/<file_id>")
def api_download_file(file_id: str):
    return download_file(file_id)


@app.post("/api/download/transcript")
def api_download_transcript():
    return download_transcript()


@app.post("/api/download/thumbnail")
def api_download_thumbnail():
    return download_thumbnail_file()


@app.post("/api/set-download-folder")
def api_set_download_folder():
    return set_download_folder()


@app.get("/api/download-folder")
def api_get_download_folder():
    return get_download_folder()


if __name__ == "__main__":
    host = _get_server_host()
    port = _get_server_port()
    lan_ip = _get_local_network_ip()
    print("Server running")
    print("")
    print("Local access:")
    print(f"http://127.0.0.1:{port}")
    print("")
    if host == "0.0.0.0":
        print("Network access:")
        print(f"http://{lan_ip}:{port}")
        print("")
    app.run(host=host, port=port, debug=False, threaded=True)
