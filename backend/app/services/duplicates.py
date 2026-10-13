from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import xxhash
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import Session

from app.core.paths import open_regular_file, resolve_library_file
from app.models import DuplicateGroup, DuplicateMember, DuplicateRun, Job, Library, MediaFile

HASH_CHUNK = 4 * 1024 * 1024
HASH_VERSION = "xxh3_64-v2"
BATCH_SIZE = 250


class HashCancelled(Exception):
    pass


def invalidate_duplicate_groups(db: Session, media_ids: Iterable[int]) -> None:
    """Drop whole groups so removing Keep never makes every survivor disposable."""
    ids = list(media_ids)
    if ids:
        affected = select(DuplicateMember.group_id).where(DuplicateMember.media_id.in_(ids))
        db.execute(delete(DuplicateGroup).where(DuplicateGroup.id.in_(affected)))


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino


def _stable_hash(
    path: Path,
    algorithm: str,
    *,
    expected: os.stat_result | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str | None:
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("哈希目标不是普通文件")
    if expected is not None and _fingerprint(before) != _fingerprint(expected):
        return None
    hasher = xxhash.xxh3_64() if algorithm == "xxh3_64" else hashlib.sha256()
    with open_regular_file(path) as handle:
        if _fingerprint(os.fstat(handle.fileno())) != _fingerprint(before):
            return None
        while chunk := handle.read(HASH_CHUNK):
            if cancelled is not None and cancelled():
                raise HashCancelled()
            hasher.update(chunk)
        if _fingerprint(os.fstat(handle.fileno())) != _fingerprint(before):
            return None
    after = path.stat(follow_symlinks=False)
    if _fingerprint(before) != _fingerprint(after):
        return None
    return hasher.hexdigest()


def _current_stat(media: MediaFile, path: Path) -> os.stat_result:
    current = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode):
        raise ValueError("文件不再是普通文件，请重新扫描")
    if current.st_size != media.size or current.st_mtime_ns != media.mtime_ns:
        raise ValueError("文件大小或修改时间已变化，请重新扫描")
    if media.file_device is not None and media.file_inode is not None:
        if (media.file_device, media.file_inode) != (current.st_dev, current.st_ino):
            raise ValueError("文件已被替换，请重新扫描")
    return current


def _keep_key(media: MediaFile, library_order: dict[int, int]) -> tuple:
    captured = media.captured_at
    if captured is None:
        captured_timestamp = float("inf")
    else:
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        captured_timestamp = captured.timestamp()
    return (
        library_order.get(media.library_id, 999999),
        captured_timestamp,
        media.mtime_ns,
        len(media.relative_path),
        media.id,
    )


def run_duplicate_detection(db: Session, job: Job) -> dict:
    run = db.get(DuplicateRun, int(job.payload["duplicate_run_id"]))
    if run is None:
        raise RuntimeError("重复检测任务不存在")
    library_ids = list(dict.fromkeys(run.library_ids))
    library_order = {value: index for index, value in enumerate(library_ids)}
    libraries = dict(
        db.execute(select(Library.id, Library.root_path).where(Library.id.in_(library_ids))).all()
    )
    types = ["photo", "video"] if run.media_type == "both" else [run.media_type]
    filters = (
        MediaFile.library_id.in_(library_ids),
        MediaFile.media_type.in_(types),
        MediaFile.status == "active",
        MediaFile.size > 0,
    )
    sizes = (
        select(MediaFile.media_type, MediaFile.size)
        .where(*filters)
        .group_by(MediaFile.media_type, MediaFile.size)
        .having(func.count(MediaFile.id) > 1)
    )
    candidates = [*filters, tuple_(MediaFile.media_type, MediaFile.size).in_(sizes)]
    db.execute(delete(DuplicateGroup).where(DuplicateGroup.run_id == run.id))
    job.total = int(db.scalar(select(func.count(MediaFile.id)).where(*candidates)) or 0)
    job.phase = "计算快速哈希"
    db.commit()
    result: dict = {"groups": 0, "files_hashed": 0, "errors": 0, "error_samples": []}

    def cancelled() -> bool:
        return bool(db.scalar(select(Job.cancel_requested).where(Job.id == job.id)))

    def progress() -> None:
        job.progress_seq += 1
        job.heartbeat_at = datetime.now(timezone.utc)
        db.commit()

    def file_error(media: MediaFile, exc: Exception) -> None:
        media.quick_hash = None
        media.quick_hash_version = None
        media.sha256 = None
        invalidate_duplicate_groups(db, [media.id])
        result["errors"] += 1
        if len(result["error_samples"]) < 20:
            result["error_samples"].append({"media_id": media.id, "error": str(exc)[:300]})

    # Disk staging bounds RAM even if 100,000 files have one size or identical content.
    with tempfile.TemporaryDirectory(prefix="photosweep-duplicates-") as staging:
        with closing(sqlite3.connect(Path(staging) / "candidates.sqlite")) as scratch:
            scratch.execute("PRAGMA temp_store=FILE")
            scratch.execute("PRAGMA cache_size=-2048")
            scratch.execute(
                "CREATE TABLE candidates (id INTEGER PRIMARY KEY, kind TEXT, size INTEGER, "
                "quick TEXT, sha TEXT, physical TEXT, library_order INTEGER, captured REAL, "
                "mtime INTEGER, path_length INTEGER)"
            )
            scratch.execute("CREATE INDEX quick_groups ON candidates(kind,size,quick)")
            scratch.execute("CREATE INDEX sha_groups ON candidates(kind,size,quick,sha)")
            last_id = 0
            try:
                while True:
                    batch = db.scalars(
                        select(MediaFile)
                        .where(*candidates, MediaFile.id > last_id)
                        .order_by(MediaFile.id)
                        .limit(BATCH_SIZE)
                    ).all()
                    if not batch:
                        break
                    for media in batch:
                        last_id = media.id
                        if cancelled():
                            raise HashCancelled()
                        try:
                            path = resolve_library_file(
                                libraries[media.library_id], media.relative_path
                            )
                            current = _current_stat(media, path)
                            if not media.quick_hash or media.quick_hash_version != HASH_VERSION:
                                digest = _stable_hash(
                                    path, "xxh3_64", expected=current, cancelled=cancelled
                                )
                                if digest is None:
                                    raise ValueError("哈希读取期间文件发生变化，请重新扫描")
                                media.quick_hash = digest
                                media.quick_hash_version = HASH_VERSION
                                media.sha256 = None
                                result["files_hashed"] += 1
                            _current_stat(media, path)
                            physical = (
                                f"{current.st_dev}:{current.st_ino}"
                                if current.st_ino
                                else f"id:{media.id}"
                            )
                            key = _keep_key(media, library_order)
                            scratch.execute(
                                "INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (
                                    media.id,
                                    media.media_type,
                                    media.size,
                                    media.quick_hash,
                                    None,
                                    physical,
                                    key[0],
                                    key[1],
                                    key[2],
                                    key[3],
                                ),
                            )
                        except HashCancelled:
                            raise
                        except Exception as exc:
                            file_error(media, exc)
                        job.processed += 1
                    scratch.commit()
                    progress()

                job.phase = "SHA-256 验证"
                progress()
                quick_groups = scratch.execute(
                    "SELECT kind,size,quick FROM candidates "
                    "GROUP BY kind,size,quick HAVING COUNT(*)>1"
                )
                for kind, size, quick in quick_groups:
                    for index, (media_id,) in enumerate(
                        scratch.execute(
                            "SELECT id FROM candidates "
                            "WHERE kind=? AND size=? AND quick=? ORDER BY id",
                            (kind, size, quick),
                        ),
                        start=1,
                    ):
                        if cancelled():
                            raise HashCancelled()
                        media = db.get(MediaFile, media_id)
                        if media is None:
                            continue
                        try:
                            path = resolve_library_file(
                                libraries[media.library_id], media.relative_path
                            )
                            current = _current_stat(media, path)
                            if not media.sha256:
                                media.sha256 = _stable_hash(
                                    path, "sha256", expected=current, cancelled=cancelled
                                )
                            if not media.sha256:
                                raise ValueError("SHA-256 读取期间文件发生变化，请重新扫描")
                            _current_stat(media, path)
                            scratch.execute(
                                "UPDATE candidates SET sha=? WHERE id=?", (media.sha256, media_id)
                            )
                        except HashCancelled:
                            raise
                        except Exception as exc:
                            file_error(media, exc)
                        if index % BATCH_SIZE == 0:
                            progress()
                    scratch.commit()
                    progress()

                job.phase = "建立重复组"
                progress()
                for kind, size, quick, sha in scratch.execute(
                    "SELECT kind,size,quick,sha FROM candidates WHERE sha IS NOT NULL "
                    "GROUP BY kind,size,quick,sha HAVING COUNT(*)>1"
                ):
                    if cancelled():
                        raise HashCancelled()
                    params = (kind, size, quick, sha)
                    predicate = "kind=? AND size=? AND quick=? AND sha=?"
                    # Revalidate immediately before publication, including cached hashes.
                    for (media_id,) in scratch.execute(
                        f"SELECT id FROM candidates WHERE {predicate}", params
                    ):
                        if cancelled():
                            raise HashCancelled()
                        media = db.get(MediaFile, media_id)
                        try:
                            if media is None or media.status != "active":
                                raise ValueError("文件已不可用")
                            path = resolve_library_file(
                                libraries[media.library_id], media.relative_path
                            )
                            _current_stat(media, path)
                        except Exception as exc:
                            scratch.execute(
                                "UPDATE candidates SET sha=NULL WHERE id=?", (media_id,)
                            )
                            if media is not None:
                                file_error(media, exc)
                    count, physical = scratch.execute(
                        "SELECT COUNT(*), COUNT(DISTINCT physical) FROM candidates "
                        f"WHERE {predicate}",
                        params,
                    ).fetchone()
                    if count < 2:
                        continue
                    keep_id = scratch.execute(
                        f"SELECT id FROM candidates WHERE {predicate} "
                        "ORDER BY library_order,captured,mtime,path_length,id LIMIT 1",
                        params,
                    ).fetchone()[0]
                    group = DuplicateGroup(
                        run_id=run.id,
                        media_type=kind,
                        size=size,
                        quick_hash=quick,
                        sha256=sha,
                        copy_count=count,
                        potential_savings=(physical - 1) * size,
                    )
                    db.add(group)
                    db.flush()
                    group_id = group.id
                    for index, (media_id, copies) in enumerate(
                        scratch.execute(
                            "SELECT id, COUNT(*) OVER (PARTITION BY physical) FROM candidates "
                            f"WHERE {predicate} ORDER BY id",
                            params,
                        ),
                        start=1,
                    ):
                        if cancelled():
                            raise HashCancelled()
                        member = DuplicateMember(
                            group_id=group_id,
                            media_id=media_id,
                            is_keep=media_id == keep_id,
                            is_hardlink=copies > 1,
                        )
                        if hasattr(DuplicateMember, "is_default_keep"):
                            member.is_default_keep = media_id == keep_id
                        db.add(member)
                        if index % BATCH_SIZE == 0:
                            db.flush()
                    result["groups"] += 1
                    progress()
            except HashCancelled:
                # Discard pending members before deleting a partly built group.
                db.rollback()
                db.execute(delete(DuplicateGroup).where(DuplicateGroup.run_id == run.id))
                db.commit()
                result.update(cancelled=True, groups=0)
            except Exception:
                db.rollback()
                db.execute(delete(DuplicateGroup).where(DuplicateGroup.run_id == run.id))
                db.commit()
                raise
    return result
