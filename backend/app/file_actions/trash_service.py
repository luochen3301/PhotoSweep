from __future__ import annotations

import errno
import hashlib
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.paths import is_relative_to, resolve_library_file, resolve_trash_file
from app.models import Classification, FileOperation, Library, MediaFile, TrashItem

COPY_CHUNK = 4 * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as handle:
        while chunk := handle.read(COPY_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_verified(source: Path, destination: Path, expected_size: int, expected_mtime: int) -> str:
    temporary = destination.with_name(f".{destination.name}.partial")
    digest = hashlib.sha256()
    try:
        with source.open("rb", buffering=0) as src, temporary.open("xb", buffering=0) as dst:
            while chunk := src.read(COPY_CHUNK):
                digest.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        source_after = source.stat(follow_symlinks=False)
        if source_after.st_size != expected_size or source_after.st_mtime_ns != expected_mtime:
            raise RuntimeError("File changed since scan. Please rescan before deleting.")
        if temporary.stat().st_size != expected_size:
            raise RuntimeError("复制后的文件大小不一致")
        source_hash = digest.hexdigest()
        if sha256_file(temporary) != source_hash:
            raise RuntimeError("复制后的 SHA-256 验证失败")
        shutil.copystat(source, temporary, follow_symlinks=False)
        temporary.replace(destination)
        return source_hash
    finally:
        temporary.unlink(missing_ok=True)


def _safe_move(
    source: Path,
    destination: Path,
    expected_size: int,
    expected_mtime: int,
) -> tuple[str, bool]:
    if destination.exists():
        raise RuntimeError("目标文件已存在")
    try:
        os.rename(source, destination)
        return sha256_file(destination), True
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    digest = _copy_verified(source, destination, expected_size, expected_mtime)
    try:
        source.unlink()
    except OSError as exc:
        raise RuntimeError(f"副本已验证，但 Source 删除失败，已保留两份: {exc}") from exc
    return digest, False


def _check_source(media: MediaFile, library: Library) -> tuple[Path, os.stat_result]:
    if media.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, f"{media.filename} 不再是活动文件")
    path = resolve_library_file(library.root_path, media.relative_path)
    stat_result = path.stat(follow_symlinks=False)
    if not path.is_file() or path.is_symlink():
        raise HTTPException(status.HTTP_409_CONFLICT, "仅允许移动普通文件")
    if stat_result.st_size != media.size or stat_result.st_mtime_ns != media.mtime_ns:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "File changed since scan. Please rescan before deleting.",
        )
    return path, stat_result


def move_to_trash(db: Session, media_ids: list[int]) -> list[TrashItem]:
    settings = get_settings()
    moved: list[TrashItem] = []
    for media_id in media_ids:
        media = db.get(MediaFile, media_id)
        if media is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"媒体 {media_id} 不存在")
        library = db.get(Library, media.library_id)
        if library is None or not library.writable:
            raise HTTPException(status.HTTP_409_CONFLICT, "Library 为只读，不能移动文件")
        source, source_stat = _check_source(media, library)
        item_uuid = str(uuid.uuid4())
        item_dir = settings.trash_dir / str(library.id) / item_uuid
        item_dir.mkdir(parents=True, exist_ok=False)
        destination = item_dir / media.filename
        relative_destination = destination.relative_to(settings.trash_dir).as_posix()
        operation = FileOperation(
            operation="move",
            state="pending",
            media_id=media.id,
            source_path=str(source),
            destination_path=str(destination),
            expected_size=media.size,
            expected_mtime_ns=media.mtime_ns,
            expected_sha256=media.sha256,
        )
        db.add(operation)
        db.commit()
        try:
            operation.state = "moving"
            db.commit()
            digest, _atomic = _safe_move(
                source, destination, source_stat.st_size, source_stat.st_mtime_ns
            )
            categories = db.scalars(
                select(Classification.category).where(Classification.media_id == media.id)
            ).all()
            trash_item = TrashItem(
                item_uuid=item_uuid,
                media_id=media.id,
                library_id=library.id,
                original_relative_path=media.relative_path,
                trash_relative_path=relative_destination,
                filename=media.filename,
                size=media.size,
                mtime_ns=media.mtime_ns,
                sha256=digest,
                category_snapshot=list(categories),
            )
            db.add(trash_item)
            media.status = "trashed"
            operation.state = "completed"
            operation.expected_sha256 = digest
            db.commit()
            moved.append(trash_item)
        except Exception as exc:
            operation.state = "failed"
            operation.error = str(exc)[:4000]
            db.commit()
            if destination.exists() and source.exists():
                # Conservative state: two verified/partial copies may exist.
                pass
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return moved


def _restore_destination(root: Path, relative_path: str) -> Path:
    requested = root.joinpath(relative_path)
    parent = requested.parent.resolve(strict=True)
    if not is_relative_to(parent, root.resolve(strict=True)):
        raise RuntimeError("恢复路径越界")
    if not requested.exists():
        return requested
    stem, suffix = requested.stem, requested.suffix
    for index in range(1, 10000):
        candidate = requested.with_name(f"{stem}_restored_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("无法生成不冲突的恢复文件名")


def restore_from_trash(db: Session, trash_item_ids: list[int]) -> list[TrashItem]:
    restored: list[TrashItem] = []
    for item_id in trash_item_ids:
        item = db.get(TrashItem, item_id)
        if item is None or item.status != "active":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Trash 项目不存在")
        library = db.get(Library, item.library_id)
        if library is None or not library.writable:
            raise HTTPException(status.HTTP_409_CONFLICT, "Library 不可写，无法恢复")
        source = resolve_trash_file(item.trash_relative_path)
        if source.stat().st_size != item.size or sha256_file(source) != item.sha256:
            raise HTTPException(status.HTTP_409_CONFLICT, "Trash 文件校验失败")
        destination = _restore_destination(Path(library.root_path), item.original_relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        operation = FileOperation(
            operation="restore",
            state="moving",
            media_id=item.media_id,
            trash_item_id=item.id,
            source_path=str(source),
            destination_path=str(destination),
            expected_size=item.size,
            expected_mtime_ns=item.mtime_ns,
            expected_sha256=item.sha256,
        )
        db.add(operation)
        db.commit()
        try:
            _safe_move(source, destination, item.size, source.stat().st_mtime_ns)
            if item.media:
                stat_result = destination.stat()
                item.media.relative_path = destination.relative_to(
                    Path(library.root_path)
                ).as_posix()
                item.media.filename = destination.name
                item.media.size = stat_result.st_size
                item.media.mtime_ns = stat_result.st_mtime_ns
                item.media.status = "active"
            item.status = "restored"
            item.restored_at = datetime.now(timezone.utc)
            operation.state = "completed"
            db.commit()
            restored.append(item)
        except Exception as exc:
            operation.state = "failed"
            operation.error = str(exc)[:4000]
            db.commit()
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return restored


def permanently_delete(db: Session, trash_item_ids: list[int]) -> int:
    deleted = 0
    trash_root = get_settings().trash_dir.resolve(strict=True)
    for item_id in trash_item_ids:
        item = db.get(TrashItem, item_id)
        if item is None or item.status != "active":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Trash 项目不存在")
        source = resolve_trash_file(item.trash_relative_path)
        expected_parent = (trash_root / str(item.library_id) / item.item_uuid).resolve()
        if source.parent != expected_parent or source.is_symlink() or not source.is_file():
            raise HTTPException(status.HTTP_409_CONFLICT, "Trash 文件路径不安全")
        if source.stat().st_size != item.size or sha256_file(source) != item.sha256:
            raise HTTPException(status.HTTP_409_CONFLICT, "Trash 文件校验失败")
        operation = FileOperation(
            operation="delete",
            state="deleting",
            media_id=item.media_id,
            trash_item_id=item.id,
            source_path=str(source),
            destination_path=None,
            expected_size=item.size,
            expected_mtime_ns=item.mtime_ns,
            expected_sha256=item.sha256,
        )
        db.add(operation)
        db.commit()
        source.unlink()
        try:
            source.parent.rmdir()
        except OSError:
            pass
        item.status = "deleted"
        item.deleted_at = datetime.now(timezone.utc)
        if item.media:
            item.media.status = "missing"
        operation.state = "completed"
        db.commit()
        deleted += 1
    return deleted
