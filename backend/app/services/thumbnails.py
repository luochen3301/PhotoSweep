from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from pathlib import Path

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.paths import resolve_library_file, resolve_trash_file
from app.models import Library, MediaFile, TrashItem

register_heif_opener()
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


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
    if size not in {256, 512}:
        raise ValueError("缩略图尺寸无效")
    source = _source_path(db, media)
    cache_key = hashlib.sha256(
        f"{media.library_id}:{media.relative_path}:{media.size}:{media.mtime_ns}:{size}".encode()
    ).hexdigest()
    destination = get_settings().cache_dir / "thumbnails" / cache_key[:2] / f"{cache_key}.webp"
    if destination.exists():
        os.utime(destination, None)
        return destination
    with _lock_for(cache_key):
        if destination.exists():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp.webp")
        try:
            if media.media_type == "photo":
                with Image.open(source) as image:
                    image.seek(0)
                    image = ImageOps.exif_transpose(image).convert("RGB")
                    image.thumbnail((size, size), Image.Resampling.LANCZOS)
                    image.save(temporary, format="WEBP", quality=82, method=4)
            else:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-ss",
                        "1",
                        "-i",
                        str(source),
                        "-frames:v",
                        "1",
                        "-vf",
                        f"scale='min({size},iw)':-2",
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
                )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return destination


def enforce_cache_quota(quota_gb: int) -> dict[str, int]:
    root = get_settings().cache_dir / "thumbnails"
    files = [path for path in root.rglob("*.webp") if path.is_file()]
    total = sum(path.stat().st_size for path in files)
    quota = quota_gb * 1024**3
    removed = 0
    if total > quota:
        for path in sorted(files, key=lambda item: item.stat().st_atime_ns):
            if total <= quota:
                break
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size
            removed += 1
    return {"bytes": total, "removed": removed}
