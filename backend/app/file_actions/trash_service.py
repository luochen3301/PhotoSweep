from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.paths import (
    open_regular_file,
    resolve_library_file,
    resolve_trash_file,
    secure_mkdir_parents,
    secure_parent,
)
from app.models import Classification, FileOperation, Library, MediaFile, TrashItem
from app.services.duplicates import invalidate_duplicate_groups

COPY_CHUNK = 4 * 1024 * 1024
file_actions_lock = threading.RLock()
_CHANGED = "File changed since scan. Please rescan before deleting."


def _fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _stat_file(path: Path) -> os.stat_result:
    with secure_parent(path) as (parent, name):
        result = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(result.st_mode):
        raise RuntimeError("仅允许普通文件")
    return result


def _check_stat(path: Path, expected: os.stat_result) -> os.stat_result:
    actual = _stat_file(path)
    if _fingerprint(actual) != _fingerprint(expected):
        raise RuntimeError(_CHANGED)
    return actual


def sha256_file(path: Path) -> str:
    before = _stat_file(path)
    digest = hashlib.sha256()
    with open_regular_file(path) as handle:
        if _fingerprint(os.fstat(handle.fileno())) != _fingerprint(before):
            raise RuntimeError(_CHANGED)
        while chunk := handle.read(COPY_CHUNK):
            digest.update(chunk)
        if _fingerprint(os.fstat(handle.fileno())) != _fingerprint(before):
            raise RuntimeError(_CHANGED)
    _check_stat(path, before)
    return digest.hexdigest()


def _fsync_parent(path: Path) -> None:
    if os.name == "posix":
        with secure_parent(path) as (parent, _name):
            os.fsync(parent)


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomic exclusive rename on Linux/Windows, exclusive link fallback elsewhere."""
    with (
        secure_parent(source) as (source_fd, source_name),
        secure_parent(destination) as (destination_fd, destination_name),
    ):
        if sys.platform == "linux":
            libc = ctypes.CDLL(None, use_errno=True)
            rename = getattr(libc, "renameat2", None)
            if rename is not None:
                rename.argtypes = [
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                ]
                rename.restype = ctypes.c_int
                result = rename(
                    source_fd,
                    os.fsencode(source_name),
                    destination_fd,
                    os.fsencode(destination_name),
                    1,
                )  # RENAME_NOREPLACE
                if result == 0:
                    return
                code = ctypes.get_errno()
                if code not in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                    raise OSError(code, os.strerror(code), str(destination))
        elif os.name == "nt":
            # Windows os.rename explicitly refuses an existing destination.
            os.rename(source, destination)
            return
        os.link(
            source_name,
            destination_name,
            src_dir_fd=source_fd,
            dst_dir_fd=destination_fd,
            follow_symlinks=False,
        )
        os.unlink(source_name, dir_fd=source_fd)


def _unlink_checked(path: Path, expected: os.stat_result) -> None:
    with secure_parent(path) as (parent, name):
        actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _fingerprint(actual) != _fingerprint(expected) or not stat.S_ISREG(actual.st_mode):
            raise RuntimeError(_CHANGED)
        os.unlink(name, dir_fd=parent)
    _fsync_parent(path)


def _copy_verified(
    source: Path,
    destination: Path,
    expected_size: int,
    expected_mtime: int,
    expected_sha256: str | None = None,
) -> str:
    before = _stat_file(source)
    if before.st_size != expected_size or before.st_mtime_ns != expected_mtime:
        raise RuntimeError(_CHANGED)
    temporary = destination.with_name(f".{uuid.uuid4().hex}.partial")
    digest = hashlib.sha256()
    created = False
    try:
        with open_regular_file(source) as src, secure_parent(temporary) as (parent, name):
            if _fingerprint(os.fstat(src.fileno())) != _fingerprint(before):
                raise RuntimeError(_CHANGED)
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            created = True
            with os.fdopen(descriptor, "wb") as dst:
                while chunk := src.read(COPY_CHUNK):
                    digest.update(chunk)
                    dst.write(chunk)
                dst.flush()
                if os.name == "posix":
                    os.utime(dst.fileno(), ns=(before.st_atime_ns, before.st_mtime_ns))
                os.fsync(dst.fileno())
            if _fingerprint(os.fstat(src.fileno())) != _fingerprint(before):
                raise RuntimeError(_CHANGED)
        if os.name != "posix":
            os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
        source_hash = digest.hexdigest()
        if expected_sha256 is not None and source_hash != expected_sha256:
            raise RuntimeError(_CHANGED)
        if _stat_file(temporary).st_size != expected_size:
            raise RuntimeError("复制后的文件大小不一致")
        if sha256_file(temporary) != source_hash:
            raise RuntimeError("复制后的 SHA-256 验证失败")
        _check_stat(source, before)
        _rename_noreplace(temporary, destination)
        created = False
        _fsync_parent(destination)
        _check_stat(source, before)
        return source_hash
    finally:
        if created:
            try:
                with secure_parent(temporary) as (parent, name):
                    os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass


def _safe_move(
    source: Path,
    destination: Path,
    expected_size: int,
    expected_mtime: int,
    expected_sha256: str | None = None,
) -> tuple[str, bool]:
    before = _stat_file(source)
    if before.st_size != expected_size or before.st_mtime_ns != expected_mtime:
        raise RuntimeError(_CHANGED)
    digest = sha256_file(source)
    if expected_sha256 is not None and digest != expected_sha256:
        raise RuntimeError("文件 SHA-256 与操作记录不一致")
    _check_stat(source, before)
    try:
        _rename_noreplace(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    else:
        _fsync_parent(destination)
        _fsync_parent(source)
        return digest, True
    digest = _copy_verified(source, destination, expected_size, expected_mtime, digest)
    try:
        _unlink_checked(source, before)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"副本已验证，但 Source 删除失败，已保留两份: {exc}") from exc
    return digest, False


def _check_source(media: MediaFile, library: Library) -> tuple[Path, os.stat_result]:
    if media.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, f"{media.filename} 不再是活动文件")
    path = resolve_library_file(library.root_path, media.relative_path)
    result = _stat_file(path)
    if result.st_size != media.size or result.st_mtime_ns != media.mtime_ns:
        raise HTTPException(status.HTTP_409_CONFLICT, _CHANGED)
    if (media.file_device is not None and media.file_device != result.st_dev) or (
        media.file_inode is not None and media.file_inode != result.st_ino
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, _CHANGED)
    if not os.access(path.parent, os.W_OK):
        raise HTTPException(status.HTTP_409_CONFLICT, "Library 为只读，不能移动文件")
    return path, result


def _trash_source(item: TrashItem, *, must_exist: bool = True) -> Path:
    source = resolve_trash_file(item.trash_relative_path, must_exist=must_exist)
    expected = Path(str(item.library_id)) / item.item_uuid / item.filename
    if Path(item.trash_relative_path) != expected or source.name != item.filename:
        raise HTTPException(status.HTTP_409_CONFLICT, "Trash 文件路径不安全")
    return source


def _validate_trash(item: TrashItem) -> tuple[Path, os.stat_result]:
    source = _trash_source(item)
    try:
        before = _stat_file(source)
        if before.st_size != item.size or sha256_file(source) != item.sha256:
            raise RuntimeError("Trash 文件校验失败")
        _check_stat(source, before)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return source, before


def _fail_operation(db: Session, operation: FileOperation, exc: Exception) -> None:
    db.rollback()
    operation.state = "failed"
    operation.error = str(exc)[:4000]
    if operation.operation == "move" and operation.trash_item_id:
        item = db.get(TrashItem, operation.trash_item_id)
        if item and item.status == "pending":
            try:
                destination = _trash_source(item, must_exist=False)
                if os.path.lexists(operation.source_path) and not os.path.lexists(destination):
                    # No trash payload was produced. Retain the audit but allow a retry.
                    item.status = "aborted"
            except (OSError, HTTPException):
                pass
    db.add(operation)
    db.commit()


def move_to_trash(db: Session, media_ids: list[int]) -> list[TrashItem]:
    moved: list[TrashItem] = []
    with file_actions_lock:
        for media_id in dict.fromkeys(media_ids):
            media = db.get(MediaFile, media_id, populate_existing=True)
            if media is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"媒体 {media_id} 不存在")
            library = db.get(Library, media.library_id)
            if library is None or not library.writable:
                raise HTTPException(status.HTTP_409_CONFLICT, "Library 为只读，不能移动文件")
            pending = db.scalar(
                select(TrashItem.id).where(
                    TrashItem.media_id == media.id, TrashItem.status == "pending"
                )
            )
            if pending is not None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT, "存在未确认的文件操作，请先检查操作日志"
                )
            source, before = _check_source(media, library)
            try:
                digest = sha256_file(source)
                _check_stat(source, before)
            except (OSError, RuntimeError) as exc:
                raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
            item_uuid = str(uuid.uuid4())
            relative = (Path(str(library.id)) / item_uuid / source.name).as_posix()
            destination = resolve_trash_file(relative, must_exist=False)
            secure_mkdir_parents(destination.parent)
            destination = resolve_trash_file(relative, must_exist=False)
            item = TrashItem(
                item_uuid=item_uuid,
                media_id=media.id,
                library_id=library.id,
                original_relative_path=media.relative_path,
                trash_relative_path=relative,
                filename=source.name,
                size=media.size,
                mtime_ns=media.mtime_ns,
                sha256=digest,
                status="pending",
                category_snapshot=list(
                    db.scalars(
                        select(Classification.category).where(Classification.media_id == media.id)
                    ).all()
                ),
            )
            db.add(item)
            db.flush()
            operation = FileOperation(
                operation="move",
                state="moving",
                media_id=media.id,
                trash_item_id=item.id,
                source_path=str(source),
                destination_path=str(destination),
                expected_size=media.size,
                expected_mtime_ns=media.mtime_ns,
                expected_sha256=digest,
            )
            db.add(operation)
            db.commit()
            try:
                _safe_move(source, destination, before.st_size, before.st_mtime_ns, digest)
                item.status = "active"
                media.status = "trashed"
                media.sha256 = digest
                invalidate_duplicate_groups(db, [media.id])
                operation.state = "completed"
                db.commit()
                moved.append(item)
            except Exception as exc:
                _fail_operation(db, operation, exc)
                raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return moved


def _restore_destination(root: Path, relative_path: str, occupied: set[str] | None = None) -> Path:
    requested = resolve_library_file(str(root), relative_path, must_exist=False)
    occupied = occupied or set()
    for index in range(10000):
        candidate = (
            requested
            if index == 0
            else requested.with_name(f"{requested.stem}_restored_{index}{requested.suffix}")
        )
        relative = candidate.relative_to(root).as_posix()
        if not os.path.lexists(candidate) and relative not in occupied:
            return candidate
    raise RuntimeError("无法生成不冲突的恢复文件名")


def _can_reuse_media(item: TrashItem) -> bool:
    media = item.media
    return bool(
        media
        and media.status == "trashed"
        and media.size == item.size
        and media.mtime_ns == item.mtime_ns
        and media.relative_path == item.original_relative_path
        and media.sha256 == item.sha256
    )


def _complete_restore(db: Session, item: TrashItem, destination: Path) -> None:
    library = db.get(Library, item.library_id)
    relative = destination.relative_to(Path(library.root_path)).as_posix()
    file_stat = _stat_file(destination)
    media = item.media
    if not _can_reuse_media(item):
        media = MediaFile(
            library_id=item.library_id,
            relative_path=relative,
            filename=destination.name,
            extension=destination.suffix.lower(),
            media_type=item.media.media_type
            if item.media
            else (
                "video"
                if destination.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".m4v"}
                else "photo"
            ),
            size=file_stat.st_size,
            mtime_ns=file_stat.st_mtime_ns,
            status="active",
            metadata_error="文件已恢复，请重新扫描以刷新元数据与分类",
        )
        db.add(media)
        db.flush()
        item.media = media
    media.relative_path = relative
    media.filename = destination.name
    media.size = file_stat.st_size
    media.mtime_ns = file_stat.st_mtime_ns
    media.file_device = file_stat.st_dev
    media.file_inode = file_stat.st_ino
    media.sha256 = item.sha256
    media.status = "active"
    invalidate_duplicate_groups(db, [media.id])
    item.status = "restored"
    item.restored_at = datetime.now(timezone.utc)


def restore_from_trash(db: Session, trash_item_ids: list[int]) -> list[TrashItem]:
    restored: list[TrashItem] = []
    with file_actions_lock:
        for item_id in dict.fromkeys(trash_item_ids):
            item = db.get(TrashItem, item_id, populate_existing=True)
            if item is None or item.status != "active":
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Trash 项目不存在")
            library = db.get(Library, item.library_id)
            if library is None or not library.writable:
                raise HTTPException(status.HTTP_409_CONFLICT, "Library 不可写，无法恢复")
            source, before = _validate_trash(item)
            reusable = _can_reuse_media(item)
            occupied = set(
                db.scalars(
                    select(MediaFile.relative_path).where(
                        MediaFile.library_id == library.id,
                        MediaFile.id != (item.media_id if reusable else -1),
                    )
                ).all()
            )
            destination = _restore_destination(
                Path(library.root_path), item.original_relative_path, occupied
            )
            secure_mkdir_parents(destination.parent)
            destination = resolve_library_file(
                library.root_path,
                destination.relative_to(Path(library.root_path)).as_posix(),
                must_exist=False,
            )
            operation = FileOperation(
                operation="restore",
                state="moving",
                media_id=item.media_id,
                trash_item_id=item.id,
                source_path=str(source),
                destination_path=str(destination),
                expected_size=item.size,
                expected_mtime_ns=before.st_mtime_ns,
                expected_sha256=item.sha256,
            )
            db.add(operation)
            db.commit()
            try:
                _safe_move(source, destination, item.size, before.st_mtime_ns, item.sha256)
                _complete_restore(db, item, destination)
                operation.state = "completed"
                db.commit()
                restored.append(item)
            except Exception as exc:
                _fail_operation(db, operation, exc)
                raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return restored


def permanently_delete(db: Session, trash_item_ids: list[int]) -> int:
    deleted = 0
    with file_actions_lock:
        for item_id in dict.fromkeys(trash_item_ids):
            item = db.get(TrashItem, item_id, populate_existing=True)
            if item is None or item.status != "active":
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Trash 项目不存在")
            source, before = _validate_trash(item)
            operation = FileOperation(
                operation="delete",
                state="deleting",
                media_id=item.media_id,
                trash_item_id=item.id,
                source_path=str(source),
                destination_path=None,
                expected_size=item.size,
                expected_mtime_ns=before.st_mtime_ns,
                expected_sha256=item.sha256,
            )
            db.add(operation)
            db.commit()
            try:
                _unlink_checked(source, before)
                item.status = "deleted"
                item.deleted_at = datetime.now(timezone.utc)
                if _can_reuse_media(item):
                    item.media.status = "missing"
                    invalidate_duplicate_groups(db, [item.media.id])
                operation.state = "completed"
                db.commit()
                deleted += 1
            except Exception as exc:
                _fail_operation(db, operation, exc)
                raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return deleted


def recover_file_operations(db: Session) -> dict[str, int]:
    """Reconcile proven completed moves after a crash. Never delete either copy."""
    recovered = uncertain = 0
    operations = db.scalars(select(FileOperation).where(FileOperation.state != "completed")).all()
    for operation in operations:
        try:
            item = db.get(TrashItem, operation.trash_item_id) if operation.trash_item_id else None
            if item is None or operation.expected_sha256 != item.sha256:
                raise RuntimeError("缺少可验证的 Trash 操作记录")
            if item.status == "aborted":
                continue
            library = db.get(Library, item.library_id)
            trash_path = _trash_source(item, must_exist=False)
            if operation.operation == "move":
                source = resolve_library_file(
                    library.root_path, item.original_relative_path, must_exist=False
                )
                destination = trash_path
            elif operation.operation == "restore":
                source = trash_path
                relative = Path(operation.destination_path).relative_to(Path(library.root_path))
                destination = resolve_library_file(
                    library.root_path, relative.as_posix(), must_exist=False
                )
            elif operation.operation == "delete":
                if str(trash_path) != operation.source_path or os.path.lexists(trash_path):
                    raise RuntimeError("删除尚未完成；保留现存文件")
                item.status = "deleted"
                item.deleted_at = datetime.now(timezone.utc)
                if _can_reuse_media(item):
                    item.media.status = "missing"
                operation.state = "completed"
                recovered += 1
                continue
            else:
                raise RuntimeError("未知文件操作")
            if (
                str(source) != operation.source_path
                or str(destination) != operation.destination_path
            ):
                raise RuntimeError("操作路径与 Trash 记录不符")
            if os.path.lexists(source) or not os.path.lexists(destination):
                if (
                    operation.operation == "move"
                    and os.path.lexists(source)
                    and not os.path.lexists(destination)
                ):
                    item.status = "aborted"
                    operation.state = "failed"
                    operation.error = "操作未移动文件，源仍存在；保留审计记录，可重新尝试"
                    db.commit()
                    continue
                raise RuntimeError("状态不明确，已保留源文件及所有副本")
            if (
                _stat_file(destination).st_size != item.size
                or sha256_file(destination) != item.sha256
            ):
                raise RuntimeError("目标文件校验失败，未删除任何文件")
            if operation.operation == "move":
                item.status = "active"
                if (
                    item.media
                    and item.media.status in {"active", "missing"}
                    and item.media.relative_path == item.original_relative_path
                    and item.media.size == item.size
                    and item.media.mtime_ns == item.mtime_ns
                ):
                    item.media.status = "trashed"
                    item.media.sha256 = item.sha256
                    invalidate_duplicate_groups(db, [item.media.id])
            else:
                _complete_restore(db, item, destination)
            operation.state = "completed"
            operation.error = None
            recovered += 1
        except Exception as exc:
            db.rollback()
            operation.state = "failed"
            operation.error = f"容器重启后保守检查：{exc}"[:4000]
            uncertain += 1
        db.commit()
    db.commit()
    return {"recovered": recovered, "uncertain": uncertain}
