from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.database import Base
from app.models import (
    Classification,
    DuplicateGroup,
    DuplicateMember,
    DuplicateRun,
    Job,
    Library,
    MediaFile,
    Setting,
)
from app.scanner import library_scanner as scanner
from app.services import duplicates
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "app.core.paths.get_settings", lambda: SimpleNamespace(allowed_media_roots=[tmp_path])
    )
    monkeypatch.setattr(scanner._exiftool, "read", lambda path: {})
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def library(db: Session, root: Path) -> Library:
    root.mkdir(parents=True, exist_ok=True)
    item = Library(name=root.name, root_path=str(root), writable=True)
    db.add(item)
    db.commit()
    return item


def scan(db: Session, item: Library, mode: str = "full", **payload) -> dict:
    job = Job(
        kind="scan",
        status="running",
        payload={
            "library_id": item.id,
            "mode": mode,
            **payload,
        },
    )
    db.add(job)
    db.commit()
    return scanner.scan_library(db, job)


def detect(db: Session, items: list[Library], media_type: str = "photo", **job_fields) -> dict:
    job = Job(kind="duplicate", status="running", payload={}, **job_fields)
    db.add(job)
    db.flush()
    run = DuplicateRun(
        job_id=job.id, library_ids=[item.id for item in items], media_type=media_type
    )
    db.add(run)
    db.flush()
    job.payload = {"duplicate_run_id": run.id}
    db.commit()
    return duplicates.run_duplicate_detection(db, job)


def png(path: Path, color: str = "red") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (480, 960), color).save(path)


def test_keep_survives_rescan_and_resets_on_changed_fingerprint(db: Session, tmp_path: Path):
    item = library(db, tmp_path / "photos")
    path = Path(item.root_path) / "Screenshot.png"
    png(path)
    scan(db, item)
    decision = db.scalar(select(Classification))
    decision.decision = "keep"
    db.commit()
    scan(db, item)
    assert db.scalar(select(Classification)).decision == "keep"
    png(path, "blue")
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000))
    scan(db, item, "incremental")
    assert db.scalar(select(Classification)).decision == "review"


def test_unreadable_directory_does_not_mark_unseen_files_missing(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    item = library(db, tmp_path / "photos")
    blocked = Path(item.root_path) / "private"
    png(blocked / "one.png")
    scan(db, item)
    real_scandir = os.scandir

    def deny(path):
        if Path(path) == blocked:
            raise PermissionError("permission denied")
        return real_scandir(path)

    monkeypatch.setattr(scanner.os, "scandir", deny)
    result = scan(db, item)
    assert result["errors"] == 1
    assert result["missing_reconciliation_skipped"]
    assert db.scalar(select(MediaFile)).status == "active"


def test_corrupt_file_is_recorded_and_stale_metadata_cleared(db: Session, tmp_path: Path):
    item = library(db, tmp_path / "photos")
    path = Path(item.root_path) / "one.png"
    png(path)
    scan(db, item)
    path.write_bytes(b"invalid-image")
    result = scan(db, item, "incremental")
    media = db.scalar(select(MediaFile))
    assert result["errors"] == 1 and media.metadata_error
    assert media.width is None and media.height is None


def test_single_rescan_does_not_mark_unrelated_media_missing(db: Session, tmp_path: Path):
    item = library(db, tmp_path / "photos")
    png(Path(item.root_path) / "a.png")
    png(Path(item.root_path) / "b.png")
    scan(db, item)
    media = db.scalar(select(MediaFile).where(MediaFile.filename == "a.png"))
    assert scan(db, item, media_id=media.id)["processed"] == 1
    assert set(db.scalars(select(MediaFile.status))) == {"active"}


def test_metadata_workers_limit_is_used(db: Session, tmp_path: Path, monkeypatch):
    item = library(db, tmp_path / "photos")
    for index in range(12):
        (Path(item.root_path) / f"{index}.png").write_bytes(b"test")
    db.add(Setting(key="metadata_workers", value={"value": 3}))
    db.commit()
    lock = threading.Lock()
    active = maximum = 0

    def metadata(path):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return {"width": 10, "height": 10}

    monkeypatch.setattr(scanner, "read_image_metadata", metadata)
    result = scan(db, item)
    assert result["errors"] == 0
    assert maximum == 3


def test_metadata_change_during_scan_is_reported(db: Session, tmp_path: Path, monkeypatch):
    item = library(db, tmp_path / "photos")
    path = Path(item.root_path) / "a.png"
    png(path)

    def mutate(path):
        path.write_bytes(b"changed")
        return {"width": 999}

    monkeypatch.setattr(scanner, "read_image_metadata", mutate)
    result = scan(db, item)
    media = db.scalar(select(MediaFile))
    assert result["errors"] == 1 and "变化" in media.metadata_error
    assert media.width is None


def test_duplicate_scope_keep_order_and_hardlinks(db: Session, tmp_path: Path):
    first, second, excluded = [library(db, tmp_path / name) for name in ("a", "b", "c")]
    original = Path(first.root_path) / "one.png"
    png(original)
    os.link(original, Path(first.root_path) / "hardlink.png")
    for item in [second, excluded]:
        (Path(item.root_path) / "copy.png").write_bytes(original.read_bytes())
    for item in [first, second, excluded]:
        scan(db, item)
    result = detect(db, [second, first])
    assert result["groups"] == 1
    group = db.scalar(select(DuplicateGroup))
    assert group.copy_count == 3
    assert group.potential_savings == original.stat().st_size
    members = db.scalars(select(DuplicateMember)).all()
    assert sum(member.is_hardlink for member in members) == 2
    assert next(member.media.library_id for member in members if member.is_keep) == second.id
    assert excluded.id not in {member.media.library_id for member in members}


def test_cached_hashes_rejected_after_unscanned_change(db: Session, tmp_path: Path):
    item = library(db, tmp_path / "photos")
    first, second = [Path(item.root_path) / name for name in ("a.png", "b.png")]
    png(first)
    second.write_bytes(first.read_bytes())
    scan(db, item)
    assert detect(db, [item])["groups"] == 1
    content = bytearray(second.read_bytes())
    content[-1] ^= 1
    second.write_bytes(content)
    info = second.stat()
    os.utime(second, ns=(info.st_atime_ns, info.st_mtime_ns + 1000))
    result = detect(db, [item])
    assert result["groups"] == 0 and result["errors"] == 1
    assert db.scalar(select(DuplicateGroup)) is None


def test_duplicate_photo_video_groups_are_separate(db: Session, tmp_path: Path, monkeypatch):
    item = library(db, tmp_path / "photos")
    for name in ["a.png", "b.png", "a.mp4", "b.mp4"]:
        (Path(item.root_path) / name).write_bytes(b"identical-content")
    monkeypatch.setattr(scanner, "read_image_metadata", lambda path: {})
    monkeypatch.setattr(scanner, "read_video_metadata", lambda path: {})
    scan(db, item)
    result = detect(db, [item], "both")
    assert result["groups"] == 2
    assert set(db.scalars(select(DuplicateGroup.media_type))) == {"photo", "video"}


def test_hash_change_during_read_drops_file(db: Session, tmp_path: Path, monkeypatch):
    item = library(db, tmp_path / "photos")
    png(Path(item.root_path) / "a.png")
    png(Path(item.root_path) / "b.png")
    scan(db, item)
    real_hash = duplicates._stable_hash

    def unstable(path, algorithm, **kwargs):
        if path.name == "b.png":
            return None
        return real_hash(path, algorithm, **kwargs)

    monkeypatch.setattr(duplicates, "_stable_hash", unstable)
    result = detect(db, [item])
    assert result["groups"] == 0 and result["errors"] == 1


def test_deleted_and_changed_scan_invalidates_duplicate_groups(db: Session, tmp_path: Path):
    item = library(db, tmp_path / "photos")
    png(Path(item.root_path) / "a.png")
    png(Path(item.root_path) / "b.png")
    scan(db, item)
    detect(db, [item])
    (Path(item.root_path) / "a.png").unlink()
    result = scan(db, item, "incremental")
    assert result["missing"] == 1
    assert db.scalar(select(DuplicateGroup)) is None


def test_cancelled_duplicate_run_has_no_partial_recommendations(db: Session, tmp_path: Path):
    item = library(db, tmp_path / "photos")
    png(Path(item.root_path) / "a.png")
    png(Path(item.root_path) / "b.png")
    scan(db, item)
    assert detect(db, [item], cancel_requested=True)["cancelled"]
    assert db.scalar(select(DuplicateGroup)) is None


def test_many_same_size_candidates_are_batched(db: Session, tmp_path: Path, monkeypatch):
    item = library(db, tmp_path / "photos")
    for index in range(17):
        png(Path(item.root_path) / f"{index}.png")
    scan(db, item)
    monkeypatch.setattr(duplicates, "BATCH_SIZE", 3)
    result = detect(db, [item])
    assert result["groups"] == 1 and result["files_hashed"] == 17
    assert db.scalar(select(DuplicateGroup)).copy_count == 17


def test_exif_orientation_and_utc_conversion(tmp_path: Path, monkeypatch):
    path = tmp_path / "camera.jpg"
    exif = Image.Exif()
    exif[274] = 6
    exif[36867] = "2026:07:24 12:00:00"
    exif[36881] = "+08:00"
    Image.new("RGB", (100, 200)).save(path, exif=exif)
    monkeypatch.setattr(scanner._exiftool, "read", lambda path: {})
    metadata = scanner.read_image_metadata(path)
    assert (metadata["width"], metadata["height"]) == (200, 100)
    assert metadata["captured_at"].isoformat() == "2026-07-24T04:00:00+00:00"


def test_exiftool_rejects_newline_argument_injection():
    assert scanner.ExifToolProcess().read(Path("a\n-execute\n.png")) == {}


def test_real_hash_checks_descriptor_stat_after_read(tmp_path: Path):
    path = tmp_path / "payload.png"
    path.write_bytes(b"same-size-content")
    original = path.stat()

    def alter_mtime():
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000))
        return False

    assert duplicates._stable_hash(path, "sha256", cancelled=alter_mtime) is None


def test_video_metadata_includes_rotation_and_capture_time(monkeypatch):
    monkeypatch.setattr(
        scanner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="""{
      "streams":[{"width":1920,"height":1080,"codec_name":"h264","avg_frame_rate":"30000/1001",
                    "side_data_list":[{"rotation":90}]}],
      "format":{"duration":"2.5","tags":{"creation_time":"2026-07-24T10:00:00Z",
                  "com.apple.quicktime.make":"Apple","com.apple.quicktime.model":"iPhone"}}
    }"""
        ),
    )
    metadata = scanner.read_video_metadata(Path("video.mov"))
    assert (metadata["width"], metadata["height"]) == (1080, 1920)
    assert metadata["duration_ms"] == 2500
    assert metadata["camera_make"] == "Apple"
    assert metadata["captured_at"].hour == 10
    assert metadata["fps"] == pytest.approx(29.97003)
