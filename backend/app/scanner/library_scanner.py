from __future__ import annotations

import atexit
import json
import os
import queue
import shutil
import stat as stat_module
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from PIL import Image
from pillow_heif import register_heif_opener
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.paths import resolve_library_file, validate_library_root
from app.detectors import detect_media
from app.models import Classification, Job, Library, MediaFile, Setting
from app.services.duplicates import invalidate_duplicate_groups

register_heif_opener()

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv"}


class ExifToolProcess:
    """Small, serialized client for ExifTool's long-lived stay_open mode."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._sequence = 0
        self._output: queue.Queue[str | None] = queue.Queue(maxsize=4096)

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
        self._output = queue.Queue(maxsize=4096)
        process, output = self._process, self._output

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    output.put(line, timeout=1)
                except queue.Full:
                    process.kill()
                    return
            try:
                output.put(None, timeout=1)
            except queue.Full:
                pass

        threading.Thread(target=read_output, daemon=True, name="photosweep-exiftool").start()
        return self._process

    def read(self, path: Path) -> dict:
        if "\n" in str(path) or "\r" in str(path):
            # stay_open uses a line-oriented argument protocol.
            return {}
        with self._lock:
            process = self._start()
            if process is None or process.stdin is None or process.stdout is None:
                return {}
            self._sequence += 1
            marker = str(self._sequence)
            try:
                process.stdin.write(
                    "-j\n-n\n-EXIF:DateTimeOriginal\n-EXIF:OffsetTimeOriginal\n"
                    "-EXIF:Make\n-EXIF:Model\n"
                    f"{path.absolute()}\n-execute{marker}\n"
                )
                process.stdin.flush()
                lines: list[str] = []
                ready = f"{{ready{marker}}}"
                deadline = time.monotonic() + 15
                length = 0
                while True:
                    line = self._output.get(timeout=max(0.01, deadline - time.monotonic()))
                    if line is None:
                        raise RuntimeError("ExifTool stopped unexpectedly")
                    if line.strip() == ready:
                        break
                    lines.append(line)
                    length += len(line)
                    if length > 1024 * 1024 or time.monotonic() >= deadline:
                        raise RuntimeError("ExifTool metadata exceeded time or size limit")
            except Exception:
                process.kill()
                process.wait(timeout=3)
                self._process = None
                raise
            payload = json.loads("".join(lines))
            return payload[0] if payload else {}

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            if process is None or process.poll() is not None:
                return
            if process.stdin is not None:
                try:
                    process.stdin.write("-stay_open\nFalse\n")
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


_exiftool = ExifToolProcess()
atexit.register(_exiftool.close)


def iter_media_files(
    root: Path, on_error: Callable[[Path, Exception], None] | None = None
) -> Iterator[Path]:
    """Depth-first streaming traversal; pending siblings never accumulate in memory."""
    stack: list[os.ScandirIterator] = []
    directories: list[Path] = []
    try:
        try:
            stack.append(os.scandir(root))
            directories.append(root)
        except OSError as exc:
            if on_error:
                on_error(root, exc)
            return
        while stack:
            try:
                entry = next(stack[-1])
            except StopIteration:
                stack.pop().close()
                directories.pop()
                continue
            except OSError as exc:
                if on_error:
                    on_error(directories[-1], exc)
                stack.pop().close()
                directories.pop()
                continue
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    safe, _writable = validate_library_root(str(path))
                    stack.append(os.scandir(safe))
                    directories.append(path)
                elif entry.is_file(follow_symlinks=False):
                    if path.suffix.lower() in PHOTO_EXTENSIONS | VIDEO_EXTENSIONS:
                        yield path
            except Exception as exc:
                if on_error:
                    on_error(path, exc)
    finally:
        for entries in stack:
            entries.close()


def _parse_exif_datetime(value: object, offset: object = None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value[:19], "%Y:%m:%d %H:%M:%S")
        if isinstance(offset, str) and offset:
            parsed = datetime.fromisoformat(parsed.isoformat() + offset)
        elif len(value) > 19:
            parsed = datetime.fromisoformat(parsed.isoformat() + value[19:])
        else:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def read_image_metadata(path: Path) -> dict:
    with Image.open(path) as image:
        width, height = image.size
        exif = image.getexif()
        if exif.get(274) in {5, 6, 7, 8}:
            width, height = height, width
        metadata = {
            "width": width,
            "height": height,
            "camera_make": str(exif.get(271, "")).strip() or None,
            "camera_model": str(exif.get(272, "")).strip() or None,
            "captured_at": _parse_exif_datetime(exif.get(36867) or exif.get(306), exif.get(36881)),
        }
    try:
        supplemental = _exiftool.read(path)
    except Exception:
        supplemental = {}
    metadata["camera_make"] = metadata["camera_make"] or supplemental.get("Make")
    metadata["camera_model"] = metadata["camera_model"] or supplemental.get("Model")
    metadata["captured_at"] = metadata["captured_at"] or _parse_exif_datetime(
        supplemental.get("DateTimeOriginal"), supplemental.get("OffsetTimeOriginal")
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
        "stream=width,height,codec_name,avg_frame_rate:"
        "stream_tags=creation_time,rotate:stream_side_data=rotation:"
        "format=duration:format_tags=creation_time,com.apple.quicktime.make,com.apple.quicktime.model",
        "-of",
        "json",
        str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
    payload = json.loads(completed.stdout)
    stream = (payload.get("streams") or [{}])[0]
    if not stream.get("width") or not stream.get("height"):
        raise ValueError("没有可解码的视频流")
    duration = float((payload.get("format") or {}).get("duration") or 0)
    tags = (payload.get("format") or {}).get("tags") or {}
    stream_tags = stream.get("tags") or {}
    captured = tags.get("creation_time") or stream_tags.get("creation_time")
    try:
        captured_at = datetime.fromisoformat(captured.replace("Z", "+00:00")) if captured else None
        if captured_at:
            captured_at = captured_at.replace(tzinfo=captured_at.tzinfo or timezone.utc)
            captured_at = captured_at.astimezone(timezone.utc)
    except (ValueError, TypeError):
        captured_at = None
    rotation = stream_tags.get("rotate", 0)
    for side_data in stream.get("side_data_list") or []:
        rotation = side_data.get("rotation", rotation)
    width, height = stream.get("width"), stream.get("height")
    if abs(float(rotation)) % 180 == 90:
        width, height = height, width
    return {
        "width": width,
        "height": height,
        "codec": stream.get("codec_name"),
        "fps": _fraction_to_float(stream.get("avg_frame_rate")),
        "duration_ms": int(duration * 1000) if duration else None,
        "captured_at": captured_at,
        "camera_make": tags.get("com.apple.quicktime.make"),
        "camera_model": tags.get("com.apple.quicktime.model"),
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
            if item.decision != "keep" or item.fingerprint != fingerprint:
                db.delete(item)


def scan_library(db: Session, job: Job) -> dict:
    library_id = int(job.payload["library_id"])
    mode = job.payload.get("mode", "incremental")
    library = db.get(Library, library_id)
    if library is None or not library.enabled:
        raise RuntimeError("Library 不存在或已停用")
    root, library.writable = validate_library_root(library.root_path)
    result: dict = {
        "processed": 0,
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "errors": 0,
        "error_samples": [],
        "missing": 0,
    }
    directory_errors = 0
    last_progress = time.monotonic()
    job.phase = "扫描目录"
    db.commit()

    def record_error(path: Path, exc: Exception, *, traversal: bool = False) -> None:
        nonlocal directory_errors
        result["errors"] += 1
        directory_errors += int(traversal)
        if len(result["error_samples"]) < 20:
            result["error_samples"].append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "error": str(exc)[:300],
                }
            )

    def traversal_error(path: Path, exc: Exception) -> None:
        record_error(path, exc, traversal=True)

    def metadata_task(
        path: Path, media_type: str, before: os.stat_result
    ) -> tuple[dict, str | None]:
        try:
            safe_path = resolve_library_file(str(root), path.relative_to(root).as_posix())
            metadata = (
                read_image_metadata(safe_path)
                if media_type == "photo"
                else read_video_metadata(safe_path)
            )
            after = path.stat(follow_symlinks=False)
            if (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_dev,
                after.st_ino,
            ):
                return {}, "元数据读取期间文件发生变化，请重新扫描"
            return metadata, None
        except Exception as exc:
            return {}, str(exc)[:1000]

    def progress() -> None:
        nonlocal last_progress
        job.processed = result["processed"]
        job.result = dict(result)
        job.progress_seq += 1
        job.heartbeat_at = datetime.now(timezone.utc)
        db.commit()
        last_progress = time.monotonic()

    fields = (
        "width",
        "height",
        "camera_make",
        "camera_model",
        "captured_at",
        "duration_ms",
        "fps",
        "codec",
    )
    pending: deque[tuple[MediaFile, Path, Future]] = deque()

    def finish_one() -> None:
        media, path, future = pending.popleft()
        metadata, error = future.result()
        for field in fields:
            setattr(media, field, metadata.get(field))
        media.metadata_error = error
        if error:
            record_error(path, ValueError(error))
            media.quick_hash = media.quick_hash_version = media.sha256 = None
            invalidate_duplicate_groups(db, [media.id])
        _update_classifications(db, media)

    workers_setting = db.get(Setting, "metadata_workers")
    workers = int(workers_setting.value.get("value", 2)) if workers_setting else 2
    workers = max(1, min(workers, 8))
    single_id = job.payload.get("media_id")
    if single_id:
        single = db.get(MediaFile, int(single_id))
        if single is None or single.library_id != library.id or single.status == "trashed":
            raise RuntimeError("媒体不存在或已移入回收站")
        paths = iter([root / single.relative_path])
        mode = "full"
    else:
        paths = iter_media_files(root, traversal_error)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="photosweep-metadata") as pool:
        try:
            for path in paths:
                if _job_cancelled(db, job.id):
                    result["cancelled"] = True
                    break
                result["processed"] += 1
                relative_path = path.relative_to(root).as_posix()
                media = db.scalar(
                    select(MediaFile).where(
                        MediaFile.library_id == library.id,
                        MediaFile.relative_path == relative_path,
                    )
                )
                # Traversal saw the entry even if it is unreadable; never infer deletion from that.
                if media is not None:
                    media.last_seen_job_id = job.id
                try:
                    path = resolve_library_file(str(root), relative_path)
                    info = path.stat(follow_symlinks=False)
                    if not stat_module.S_ISREG(info.st_mode):
                        raise ValueError("不是普通媒体文件")
                    suffix = path.suffix.lower()
                    media_type = "photo" if suffix in PHOTO_EXTENSIONS else "video"
                    unchanged = (
                        media is not None
                        and media.size == info.st_size
                        and media.mtime_ns == info.st_mtime_ns
                        and media.file_device == info.st_dev
                        and media.file_inode == info.st_ino
                        and media.status == "active"
                    )
                    if unchanged and mode == "incremental" and not media.metadata_error:
                        # Cheap rules can upgrade without rereading stable media metadata.
                        _update_classifications(db, media)
                        result["unchanged"] += 1
                    else:
                        if media is None:
                            media = MediaFile(
                                library_id=library.id,
                                relative_path=relative_path,
                                filename=path.name,
                                extension=suffix,
                                media_type=media_type,
                                size=info.st_size,
                                mtime_ns=info.st_mtime_ns,
                            )
                            db.add(media)
                            result["added"] += 1
                        else:
                            result["updated"] += 1
                            if not unchanged:
                                invalidate_duplicate_groups(db, [media.id])
                        if not unchanged:
                            media.quick_hash = None
                            media.quick_hash_version = None
                            media.sha256 = None
                        media.filename, media.extension, media.media_type = (
                            path.name,
                            suffix,
                            media_type,
                        )
                        media.status = "active"
                        media.size, media.mtime_ns = info.st_size, info.st_mtime_ns
                        media.file_device, media.file_inode = info.st_dev, info.st_ino
                        media.filesystem_created_at = datetime.fromtimestamp(
                            info.st_ctime, tz=timezone.utc
                        )
                        media.last_seen_job_id = job.id
                        db.flush()
                        pending.append(
                            (media, path, pool.submit(metadata_task, path, media_type, info))
                        )
                        if len(pending) >= workers * 2:
                            finish_one()
                except Exception as exc:
                    record_error(path, exc)
                    if media is not None:
                        media.metadata_error = str(exc)[:1000]
                        invalidate_duplicate_groups(db, [media.id])
                if result["processed"] % 250 == 0 or time.monotonic() - last_progress >= 1:
                    while pending:
                        finish_one()
                    progress()
            while pending:
                finish_one()
        finally:
            if hasattr(paths, "close"):
                paths.close()

    if _job_cancelled(db, job.id):
        result["cancelled"] = True
    # A failed directory enumeration cannot prove that unseen files were deleted.
    if not result.get("cancelled") and not single_id and not directory_errors:
        while True:
            missing = db.scalars(
                select(MediaFile)
                .where(
                    MediaFile.library_id == library.id,
                    MediaFile.status == "active",
                    or_(MediaFile.last_seen_job_id.is_(None), MediaFile.last_seen_job_id != job.id),
                )
                .limit(250)
            ).all()
            if not missing:
                break
            invalidate_duplicate_groups(db, [item.id for item in missing])
            for item in missing:
                item.status = "missing"
                item.quick_hash = item.quick_hash_version = item.sha256 = None
            result["missing"] += len(missing)
            db.flush()
    result["missing_reconciliation_skipped"] = bool(directory_errors or result.get("cancelled"))
    if not result.get("cancelled") and not single_id and not directory_errors:
        library.last_scan_at = datetime.now(timezone.utc)
    job.total = result["processed"]
    progress()
    return result
