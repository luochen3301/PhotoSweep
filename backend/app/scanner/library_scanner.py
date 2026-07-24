from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from PIL import Image
from pillow_heif import register_heif_opener
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.detectors import detect_media
from app.models import Classification, Job, Library, MediaFile

register_heif_opener()

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv"}


class ExifToolProcess:
    """Small, serialized client for ExifTool's long-lived stay_open mode."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._sequence = 0

    def _start(self) -> subprocess.Popen[str] | None:
        if self._process is not None and self._process.poll() is None:
            return self._process
        executable = shutil.which("exiftool")
        if executable is None:
            return None
        self._process = subprocess.Popen(
            [executable, "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        return self._process

    def read(self, path: Path) -> dict:
        with self._lock:
            process = self._start()
            if process is None or process.stdin is None or process.stdout is None:
                return {}
            self._sequence += 1
            marker = str(self._sequence)
            process.stdin.write(
                "-j\n-n\n-EXIF:DateTimeOriginal\n-EXIF:Make\n-EXIF:Model\n"
                f"{path}\n-execute{marker}\n"
            )
            process.stdin.flush()
            lines: list[str] = []
            ready = f"{{ready{marker}}}"
            while True:
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError("ExifTool stopped unexpectedly")
                if line.strip() == ready:
                    break
                lines.append(line)
            payload = json.loads("".join(lines))
            return payload[0] if payload else {}

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            if process is None or process.poll() is not None:
                return
            if process.stdin is not None:
                process.stdin.write("-stay_open\nFalse\n")
                process.stdin.flush()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


_exiftool = ExifToolProcess()
atexit.register(_exiftool.close)


def iter_media_files(root: Path) -> Iterator[Path]:
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            suffix = Path(entry.name).suffix.lower()
                            if suffix in PHOTO_EXTENSIONS | VIDEO_EXTENSIONS:
                                yield Path(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue


def _parse_exif_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def read_image_metadata(path: Path) -> dict:
    with Image.open(path) as image:
        width, height = image.size
        exif = image.getexif()
        metadata = {
            "width": width,
            "height": height,
            "camera_make": str(exif.get(271, "")).strip() or None,
            "camera_model": str(exif.get(272, "")).strip() or None,
            "captured_at": _parse_exif_datetime(exif.get(36867) or exif.get(306)),
        }
    try:
        supplemental = _exiftool.read(path)
    except Exception:
        supplemental = {}
    metadata["camera_make"] = metadata["camera_make"] or supplemental.get("Make")
    metadata["camera_model"] = metadata["camera_model"] or supplemental.get("Model")
    metadata["captured_at"] = metadata["captured_at"] or _parse_exif_datetime(
        supplemental.get("DateTimeOriginal")
    )
    return metadata


def _fraction_to_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            return float(numerator) / float(denominator)
        return float(value)
    except (ValueError, ZeroDivisionError):
        return None


def read_video_metadata(path: Path) -> dict:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,codec_name,avg_frame_rate:format=duration",
        "-of",
        "json",
        str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
    payload = json.loads(completed.stdout)
    stream = (payload.get("streams") or [{}])[0]
    duration = float((payload.get("format") or {}).get("duration") or 0)
    return {
        "width": stream.get("width"),
        "height": stream.get("height"),
        "codec": stream.get("codec_name"),
        "fps": _fraction_to_float(stream.get("avg_frame_rate")),
        "duration_ms": int(duration * 1000) if duration else None,
    }


def _job_cancelled(db: Session, job_id: int) -> bool:
    return bool(db.scalar(select(Job.cancel_requested).where(Job.id == job_id)))


def _update_classifications(db: Session, media: MediaFile) -> None:
    fingerprint = f"{media.size}:{media.mtime_ns}"
    detections = detect_media(
        media_type=media.media_type,
        filename=media.filename,
        relative_path=media.relative_path,
        extension=media.extension,
        width=media.width,
        height=media.height,
        camera_make=media.camera_make,
        camera_model=media.camera_model,
        fps=media.fps,
        codec=media.codec,
    )
    current = {item.category: item for item in media.classifications}
    seen: set[str] = set()
    for detection in detections:
        seen.add(detection.category)
        item = current.get(detection.category)
        if item is None:
            item = Classification(media=media, category=detection.category)
            db.add(item)
        if item.fingerprint != fingerprint:
            item.decision = "review"
            item.decided_at = None
        item.confidence = detection.confidence
        item.reasons = detection.reasons
        item.detector_version = detection.detector_version
        item.app_source = detection.app_source
        item.fingerprint = fingerprint
    for category, item in current.items():
        if category not in seen:
            db.delete(item)


def scan_library(db: Session, job: Job) -> dict:
    library_id = int(job.payload["library_id"])
    mode = job.payload.get("mode", "incremental")
    library = db.get(Library, library_id)
    if library is None:
        raise RuntimeError("Library 不存在")
    root = Path(library.root_path).resolve(strict=True)
    processed = 0
    added = 0
    updated_count = 0
    skipped = 0
    errors = 0
    job.phase = "扫描目录"
    db.commit()

    for path in iter_media_files(root):
        if processed % 50 == 0 and _job_cancelled(db, job.id):
            return {"cancelled": True, "processed": processed}
        processed += 1
        relative_path = path.relative_to(root).as_posix()
        try:
            stat = path.stat(follow_symlinks=False)
            suffix = path.suffix.lower()
            media_type = "photo" if suffix in PHOTO_EXTENSIONS else "video"
            media = db.scalar(
                select(MediaFile).where(
                    MediaFile.library_id == library.id,
                    MediaFile.relative_path == relative_path,
                )
            )
            unchanged = (
                media is not None
                and media.size == stat.st_size
                and media.mtime_ns == stat.st_mtime_ns
                and media.status == "active"
            )
            if unchanged and mode == "incremental":
                media.last_seen_job_id = job.id
                skipped += 1
            else:
                metadata: dict = {}
                metadata_error = None
                try:
                    metadata = (
                        read_image_metadata(path)
                        if media_type == "photo"
                        else read_video_metadata(path)
                    )
                except Exception as exc:  # malformed media must not stop a scan
                    metadata_error = str(exc)[:1000]
                    errors += 1
                if media is None:
                    media = MediaFile(
                        library_id=library.id,
                        relative_path=relative_path,
                        filename=path.name,
                        extension=suffix,
                        media_type=media_type,
                        size=stat.st_size,
                        mtime_ns=stat.st_mtime_ns,
                    )
                    db.add(media)
                    added += 1
                else:
                    updated_count += 1
                changed = media.size != stat.st_size or media.mtime_ns != stat.st_mtime_ns
                media.filename = path.name
                media.extension = suffix
                media.media_type = media_type
                media.status = "active"
                media.size = stat.st_size
                media.mtime_ns = stat.st_mtime_ns
                media.file_device = stat.st_dev
                media.file_inode = stat.st_ino
                media.filesystem_created_at = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc)
                media.metadata_error = metadata_error
                media.last_seen_job_id = job.id
                for key, value in metadata.items():
                    setattr(media, key, value)
                if changed:
                    media.quick_hash = None
                    media.quick_hash_version = None
                    media.sha256 = None
                db.flush()
                _update_classifications(db, media)
        except Exception:
            errors += 1
        if processed % 250 == 0:
            job.processed = processed
            job.progress_seq += 1
            job.heartbeat_at = datetime.now(timezone.utc)
            db.commit()

    db.execute(
        update(MediaFile)
        .where(
            MediaFile.library_id == library.id,
            MediaFile.status == "active",
            MediaFile.last_seen_job_id != job.id,
        )
        .values(status="missing")
    )
    library.last_scan_at = datetime.now(timezone.utc)
    job.processed = processed
    job.total = processed
    job.progress_seq += 1
    db.commit()
    return {
        "processed": processed,
        "added": added,
        "updated": updated_count,
        "unchanged": skipped,
        "errors": errors,
    }
