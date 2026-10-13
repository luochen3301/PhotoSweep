from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.paths import open_regular_file, resolve_library_file, resolve_trash_file
from app.models import Library, MediaFile, Setting, TrashItem

register_heif_opener()
_locks = [threading.Lock() for _ in range(64)]
_generation_slots = threading.BoundedSemaphore(2)
_quota_guard = threading.Lock()
_last_quota_check = 0.0


def _lock_for(key: str) -> threading.Lock:
    return _locks[int(key[:8], 16) % len(_locks)]


def _source_path(db: Session, media: MediaFile) -> Path:
    library = db.get(Library, media.library_id)
    if library is None:
        raise FileNotFoundError("Library 不存在")
    if media.status == "trashed":
        trash_item = db.scalar(
            select(TrashItem).where(
                TrashItem.media_id == media.id,
                TrashItem.status == "active",
            )
        )
        if trash_item is None:
            raise FileNotFoundError("Trash 文件不存在")
        return resolve_trash_file(trash_item.trash_relative_path)
    return resolve_library_file(library.root_path, media.relative_path)


def thumbnail_path(db: Session, media: MediaFile, size: int) -> Path:
    if media.status == "trashed":
        item = db.scalar(
            select(TrashItem).where(TrashItem.media_id == media.id, TrashItem.status == "active")
        )
        if item is None:
            raise FileNotFoundError("Trash 文件不存在")
        return trash_thumbnail_path(db, item, size)
    if media.status not in {"active", "error"}:
        raise FileNotFoundError("媒体文件不可用")
    source = _source_path(db, media)
    key = f"media:{media.library_id}:{media.relative_path}:{media.size}:{media.mtime_ns}"
    return _thumbnail(db, source, media.media_type, media.size, media.mtime_ns, key, size)


def trash_thumbnail_path(db: Session, item: TrashItem, size: int) -> Path:
    if item.status != "active":
        raise FileNotFoundError("Trash 文件不存在")
    expected = Path(str(item.library_id)) / item.item_uuid / item.filename
    if Path(item.trash_relative_path) != expected:
        raise ValueError("Trash 文件路径不安全")
    source = resolve_trash_file(item.trash_relative_path)
    if source.name != item.filename:
        raise ValueError("Trash 文件路径不安全")
    media_type = (
        item.media.media_type
        if item.media
        else (
            "video"
            if source.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".m4v"}
            else "photo"
        )
    )
    key = f"trash:{item.item_uuid}:{item.size}:{item.sha256}"
    return _thumbnail(db, source, media_type, item.size, item.mtime_ns, key, size)


def _thumbnail(
    db: Session,
    source: Path,
    media_type: str,
    expected_size: int,
    expected_mtime: int,
    key: str,
    size: int,
) -> Path:
    if size not in {256, 512}:
        raise ValueError("缩略图尺寸无效")
    before = source.stat(follow_symlinks=False)
    if before.st_size != expected_size or before.st_mtime_ns != expected_mtime:
        raise ValueError("文件已改变，请重新扫描")
    cache_key = hashlib.sha256(f"{key}:{size}:v2".encode()).hexdigest()
    destination = get_settings().cache_dir / "thumbnails" / cache_key[:2] / f"{cache_key}.webp"
    if destination.exists():
        os.utime(destination, None)
        return destination
    with _lock_for(cache_key), _generation_slots:
        if destination.exists():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{uuid.uuid4().hex}.tmp.webp")
        try:
            with open_regular_file(source) as handle:
                if media_type == "photo":
                    with Image.open(handle) as image:
                        image.seek(0)
                        image = ImageOps.exif_transpose(image).convert("RGB")
                        image.thumbnail((size, size), Image.Resampling.LANCZOS)
                        image.save(temporary, format="WEBP", quality=82, method=4)
                else:
                    input_path = (
                        f"/proc/self/fd/{handle.fileno()}" if os.name == "posix" else str(source)
                    )
                    options = {"pass_fds": (handle.fileno(),)} if os.name == "posix" else {}
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-v",
                            "error",
                            "-ss",
                            "0",
                            "-i",
                            input_path,
                            "-frames:v",
                            "1",
                            "-vf",
                            f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                            "-c:v",
                            "libwebp",
                            "-quality",
                            "82",
                            "-f",
                            "image2",
                            "-y",
                            str(temporary),
                        ],
                        capture_output=True,
                        timeout=60,
                        check=True,
                        **options,
                    )
            after = source.stat(follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError("文件已改变，请重新扫描")
            if not temporary.exists() or temporary.stat().st_size == 0:
                raise ValueError("未提取到可解码帧")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    _maybe_enforce_quota(db)
    return destination


def _maybe_enforce_quota(db: Session) -> None:
    global _last_quota_check
    if time.monotonic() - _last_quota_check < 60 or not _quota_guard.acquire(blocking=False):
        return
    try:
        setting = db.get(Setting, "thumbnail_cache_gb")
        quota = int(setting.value.get("value", 10)) if setting else 10
        enforce_cache_quota(quota)
        _last_quota_check = time.monotonic()
    finally:
        _quota_guard.release()


def enforce_cache_quota(quota_gb: int) -> dict[str, int]:
    root = get_settings().cache_dir / "thumbnails"
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in names:
            path = Path(directory) / name
            if name.endswith(".webp") and not name.startswith(".") and not path.is_symlink():
                try:
                    file_stat = path.stat()
                    files.append((file_stat.st_atime_ns, file_stat.st_size, path))
                except FileNotFoundError:
                    continue
    total = sum(size for _atime, size, _path in files)
    quota = quota_gb * 1024**3
    removed = 0
    if total > quota:
        for _atime, size, path in sorted(files):
            if total <= quota:
                break
            try:
                path.unlink()
                total -= size
                removed += 1
            except FileNotFoundError:
                total -= size
            except OSError:
                continue
    return {"bytes": total, "removed": removed}
