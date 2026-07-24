from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import xxhash
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.paths import resolve_library_file
from app.models import (
    DuplicateGroup,
    DuplicateMember,
    DuplicateRun,
    Job,
    Library,
    MediaFile,
)

HASH_CHUNK = 4 * 1024 * 1024


def _stable_hash(path: Path, algorithm: str) -> str | None:
    before = path.stat(follow_symlinks=False)
    hasher = xxhash.xxh3_64() if algorithm == "xxh3_64" else hashlib.sha256()
    with path.open("rb", buffering=0) as handle:
        while chunk := handle.read(HASH_CHUNK):
            hasher.update(chunk)
    after = path.stat(follow_symlinks=False)
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        return None
    return hasher.hexdigest()


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
    run_id = int(job.payload["duplicate_run_id"])
    run = db.get(DuplicateRun, run_id)
    if run is None:
        raise RuntimeError("重复检测任务不存在")
    library_ids = run.library_ids
    library_order = {library_id: index for index, library_id in enumerate(library_ids)}
    libraries = {
        library.id: library
        for library in db.scalars(select(Library).where(Library.id.in_(library_ids))).all()
    }
    types = ["photo", "video"] if run.media_type == "both" else [run.media_type]
    db.execute(delete(DuplicateGroup).where(DuplicateGroup.run_id == run.id))
    db.commit()

    size_rows = db.execute(
        select(MediaFile.size, func.count(MediaFile.id))
        .where(
            MediaFile.library_id.in_(library_ids),
            MediaFile.media_type.in_(types),
            MediaFile.status == "active",
            MediaFile.size > 0,
        )
        .group_by(MediaFile.size)
        .having(func.count(MediaFile.id) > 1)
    ).all()
    candidate_sizes = [row[0] for row in size_rows]
    job.phase = "计算快速哈希"
    job.total = len(candidate_sizes)
    db.commit()

    groups_created = 0
    candidates_processed = 0
    for size_index, size in enumerate(candidate_sizes, start=1):
        db.refresh(job)
        if job.cancel_requested:
            return {"cancelled": True, "groups": groups_created}
        media_items = db.scalars(
            select(MediaFile).where(
                MediaFile.library_id.in_(library_ids),
                MediaFile.media_type.in_(types),
                MediaFile.status == "active",
                MediaFile.size == size,
            )
        ).all()
        by_quick: dict[str, list[MediaFile]] = defaultdict(list)
        for media in media_items:
            library = libraries.get(media.library_id)
            if library is None:
                continue
            path = resolve_library_file(library.root_path, media.relative_path)
            if not media.quick_hash or media.quick_hash_version != "xxh3_64-v1":
                digest = _stable_hash(path, "xxh3_64")
                if digest is None:
                    continue
                media.quick_hash = digest
                media.quick_hash_version = "xxh3_64-v1"
            by_quick[media.quick_hash].append(media)
            candidates_processed += 1

        for quick_hash, quick_items in by_quick.items():
            if len(quick_items) < 2:
                continue
            job.phase = "SHA-256 验证"
            by_sha: dict[str, list[MediaFile]] = defaultdict(list)
            for media in quick_items:
                library = libraries[media.library_id]
                path = resolve_library_file(library.root_path, media.relative_path)
                if not media.sha256:
                    media.sha256 = _stable_hash(path, "sha256")
                if media.sha256:
                    by_sha[media.sha256].append(media)
            for sha256, exact_items in by_sha.items():
                if len(exact_items) < 2:
                    continue
                keep = min(exact_items, key=lambda item: _keep_key(item, library_order))
                physical_keys: set[tuple[int | None, int | None]] = set()
                for item in exact_items:
                    physical_keys.add((item.file_device, item.file_inode))
                physical_copies = len(physical_keys)
                group = DuplicateGroup(
                    run_id=run.id,
                    media_type=exact_items[0].media_type,
                    size=size,
                    quick_hash=quick_hash,
                    sha256=sha256,
                    copy_count=len(exact_items),
                    potential_savings=max(physical_copies - 1, 0) * size,
                )
                db.add(group)
                db.flush()
                seen_physical: set[tuple[int | None, int | None]] = set()
                for item in exact_items:
                    physical = (item.file_device, item.file_inode)
                    hardlink = physical in seen_physical
                    seen_physical.add(physical)
                    db.add(
                        DuplicateMember(
                            group_id=group.id,
                            media_id=item.id,
                            is_keep=item.id == keep.id,
                            is_hardlink=hardlink,
                        )
                    )
                groups_created += 1
        job.processed = size_index
        job.progress_seq += 1
        job.heartbeat_at = datetime.now(timezone.utc)
        db.commit()
    return {"groups": groups_created, "files_hashed": candidates_processed}
