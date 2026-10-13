from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from app.core.config import get_settings
from app.core.paths import open_regular_file, resolve_library_file, resolve_trash_file
from app.file_actions import trash_service as service
from app.models import FileOperation, TrashItem
from app.services import thumbnails
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select
from test_trash_safety import add_media, session


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "allowed_media_roots", [tmp_path])
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    settings.trash_dir.mkdir()
    settings.cache_dir.mkdir()


def force_cross_device(monkeypatch: pytest.MonkeyPatch, original: Path) -> None:
    rename = service._rename_noreplace

    def cross_device(source: Path, destination: Path) -> None:
        if source == original:
            raise OSError(errno.EXDEV, "cross-device")
        rename(source, destination)

    monkeypatch.setattr(service, "_rename_noreplace", cross_device)


@pytest.mark.parametrize("relative", ["../escape.jpg", "sub/../escape.jpg", "/escape.jpg"])
def test_media_paths_reject_traversal(tmp_path: Path, relative: str) -> None:
    with pytest.raises(HTTPException) as error:
        resolve_library_file(str(tmp_path), relative, must_exist=False)
    assert error.value.status_code == 400


def test_symlink_rejected_before_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "linked"
    folder.mkdir()
    image = folder / "photo.jpg"
    image.write_bytes(b"photo")
    real_check = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == folder or real_check(path))
    with pytest.raises(HTTPException, match="符号链接"):
        resolve_library_file(str(tmp_path), "linked/photo.jpg")
    with pytest.raises(HTTPException, match="符号链接"):
        resolve_library_file(str(tmp_path), "linked/new.jpg", must_exist=False)


def test_trashed_file_symlink_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = get_settings().trash_dir / "linked.jpg"
    target.write_bytes(b"photo")
    real_check = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == target or real_check(path))
    with pytest.raises(HTTPException, match="符号链接"):
        resolve_trash_file("linked.jpg")


def test_real_symlink_is_never_read(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"photo")
    link = tmp_path / "link.jpg"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("Windows 当前进程无创建符号链接权限")
    with pytest.raises(HTTPException, match="符号链接"):
        resolve_library_file(str(tmp_path), "link.jpg")
    with pytest.raises((OSError, RuntimeError)):
        service.sha256_file(link)
    assert source.read_bytes() == b"photo"


def test_move_never_overwrites_existing_target(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"source")
    target.write_bytes(b"target")
    before = source.stat()
    with pytest.raises(FileExistsError):
        service._safe_move(source, target, before.st_size, before.st_mtime_ns)
    assert source.read_bytes() == b"source"
    assert target.read_bytes() == b"target"


def test_target_appearing_at_rename_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"source")
    before = source.stat()
    rename = service._rename_noreplace

    def racing_rename(src: Path, dst: Path) -> None:
        dst.write_bytes(b"arrived-concurrently")
        rename(src, dst)

    monkeypatch.setattr(service, "_rename_noreplace", racing_rename)
    with pytest.raises(FileExistsError):
        service._safe_move(source, target, before.st_size, before.st_mtime_ns)
    assert source.exists()
    assert target.read_bytes() == b"arrived-concurrently"


def test_cross_device_move_verifies_and_preserves_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"source-content")
    before = source.stat()
    force_cross_device(monkeypatch, source)
    digest, atomic = service._safe_move(source, target, before.st_size, before.st_mtime_ns)
    assert atomic is False
    assert not source.exists()
    assert target.read_bytes() == b"source-content"
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    assert digest == service.sha256_file(target)


def test_cross_device_fsync_failure_keeps_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"source")
    before = source.stat()
    force_cross_device(monkeypatch, source)

    def fail_fsync(_fd: int) -> None:
        raise OSError("interrupted copy")

    monkeypatch.setattr(service.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="interrupted"):
        service._safe_move(source, target, before.st_size, before.st_mtime_ns)
    assert source.read_bytes() == b"source"
    assert not target.exists()
    assert not list(tmp_path.glob("*.partial"))


def test_cross_device_hash_mismatch_keeps_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"source")
    before = source.stat()
    force_cross_device(monkeypatch, source)
    original_hash = service.sha256_file
    monkeypatch.setattr(
        service,
        "sha256_file",
        lambda path: "invalid" if path.suffix == ".partial" else original_hash(path),
    )
    with pytest.raises(RuntimeError, match="SHA-256"):
        service._safe_move(source, target, before.st_size, before.st_mtime_ns)
    assert source.read_bytes() == b"source"
    assert not target.exists()


def test_source_changed_during_target_verification_is_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"source")
    before = source.stat()
    force_cross_device(monkeypatch, source)
    original_hash = service.sha256_file

    def change_source(path: Path) -> str:
        digest = original_hash(path)
        if path.suffix == ".partial":
            source.write_bytes(b"modified-source")
        return digest

    monkeypatch.setattr(service, "sha256_file", change_source)
    with pytest.raises(RuntimeError, match="File changed"):
        service._safe_move(source, target, before.st_size, before.st_mtime_ns)
    assert source.read_bytes() == b"modified-source"
    assert not target.exists()


def test_source_unlink_failure_retains_two_copies_and_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session() as db:
        media = add_media(db, tmp_path / "library")
        source = Path(media.library.root_path) / media.relative_path
        force_cross_device(monkeypatch, source)

        def fail_unlink(_path: Path, _stat: os.stat_result) -> None:
            raise PermissionError("source busy")

        monkeypatch.setattr(service, "_unlink_checked", fail_unlink)
        with pytest.raises(HTTPException, match="已保留两份"):
            service.move_to_trash(db, [media.id])
        operation = db.scalar(select(FileOperation))
        item = db.scalar(select(TrashItem))
        assert operation.state == "failed"
        assert item.status == "pending"
        assert source.read_bytes() == Path(operation.destination_path).read_bytes()
        result = service.recover_file_operations(db)
        assert result == {"recovered": 0, "uncertain": 1}
        assert source.exists() and Path(operation.destination_path).exists()
        with pytest.raises(HTTPException, match="未确认"):
            service.move_to_trash(db, [media.id])


def test_failed_move_without_target_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session() as db:
        media = add_media(db, tmp_path / "library")
        move = service._safe_move

        def fail_before_move(*args, **kwargs):
            raise PermissionError("denied before move")

        monkeypatch.setattr(service, "_safe_move", fail_before_move)
        with pytest.raises(HTTPException, match="denied"):
            service.move_to_trash(db, [media.id])
        item = db.scalar(select(TrashItem))
        assert item.status == "aborted"
        assert service.recover_file_operations(db) == {"recovered": 0, "uncertain": 0}
        monkeypatch.setattr(service, "_safe_move", move)
        retried = service.move_to_trash(db, [media.id])[0]
        assert retried.status == "active"


def test_restart_recovers_move_after_filesystem_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session() as db:
        media = add_media(db, tmp_path / "library")
        move = service._safe_move

        def crash_after_move(*args, **kwargs):
            move(*args, **kwargs)
            raise RuntimeError("simulated process interruption")

        monkeypatch.setattr(service, "_safe_move", crash_after_move)
        with pytest.raises(HTTPException, match="simulated"):
            service.move_to_trash(db, [media.id])
        assert service.recover_file_operations(db) == {"recovered": 1, "uncertain": 0}
        item = db.scalar(select(TrashItem))
        assert item.status == "active"
        assert media.status == "trashed"
        assert service.recover_file_operations(db) == {"recovered": 0, "uncertain": 0}


def test_restart_recovers_restore_after_filesystem_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session() as db:
        media = add_media(db, tmp_path / "library")
        item = service.move_to_trash(db, [media.id])[0]
        move = service._safe_move

        def crash_after_restore(*args, **kwargs):
            move(*args, **kwargs)
            raise RuntimeError("interrupted restore")

        monkeypatch.setattr(service, "_safe_move", crash_after_restore)
        with pytest.raises(HTTPException, match="interrupted"):
            service.restore_from_trash(db, [item.id])
        assert service.recover_file_operations(db) == {"recovered": 1, "uncertain": 0}
        assert media.status == "active" and item.status == "restored"
        assert (tmp_path / "library" / "photo.jpg").read_bytes() == b"safe-photo-content"


def test_restart_recovers_delete_after_filesystem_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session() as db:
        media = add_media(db, tmp_path / "library")
        item = service.move_to_trash(db, [media.id])[0]
        unlink = service._unlink_checked

        def crash_after_delete(*args, **kwargs):
            unlink(*args, **kwargs)
            raise RuntimeError("interrupted delete")

        monkeypatch.setattr(service, "_unlink_checked", crash_after_delete)
        with pytest.raises(HTTPException, match="interrupted"):
            service.permanently_delete(db, [item.id])
        assert service.recover_file_operations(db) == {"recovered": 1, "uncertain": 0}
        assert item.status == "deleted" and media.status == "missing"


def test_hash_detects_replaced_inode_with_same_size_and_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"original")
    before = source.stat()
    original_open = service.open_regular_file

    @contextmanager
    def replace_after_read(path: Path):
        with original_open(path) as handle:
            yield handle
        replacement = path.with_name("replacement.jpg")
        replacement.write_bytes(b"modified")
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, path)

    monkeypatch.setattr(service, "open_regular_file", replace_after_read)
    with pytest.raises(RuntimeError, match="File changed"):
        service.sha256_file(source)
    assert source.read_bytes() == b"modified"


@pytest.mark.skipif(os.name != "posix", reason="Linux dir_fd/O_NOFOLLOW race protection")
def test_replaced_parent_symlink_cannot_redirect_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "album"
    parent.mkdir()
    source = parent / "photo.jpg"
    source.write_bytes(b"allowed")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "photo.jpg").write_bytes(b"secret-outside")
    real_open = os.open
    attacked = False

    def replace_parent(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal attacked
        if name == "photo.jpg" and dir_fd is not None and not attacked:
            attacked = True
            parent.rename(tmp_path / "moved-album")
            parent.symlink_to(outside, target_is_directory=True)
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_parent)
    with open_regular_file(source) as handle:
        assert handle.read() == b"allowed"
    assert attacked


def test_restore_conflict_preserves_both_files(tmp_path: Path) -> None:
    with session() as db:
        media = add_media(db, tmp_path / "library")
        original = Path(media.library.root_path) / media.relative_path
        item = service.move_to_trash(db, [media.id])[0]
        original.write_bytes(b"new-photo")
        service.restore_from_trash(db, [item.id])
        assert original.read_bytes() == b"new-photo"
        assert original.with_name("photo_restored_1.jpg").read_bytes() == b"safe-photo-content"
        assert media.relative_path == "photo_restored_1.jpg"


def test_restore_recreates_missing_parent(tmp_path: Path) -> None:
    with session() as db:
        media = add_media(db, tmp_path / "library", "album/photo.jpg")
        item = service.move_to_trash(db, [media.id])[0]
        (tmp_path / "library" / "album").rmdir()
        service.restore_from_trash(db, [item.id])
        assert (tmp_path / "library" / "album" / "photo.jpg").exists()


def test_restore_does_not_reassign_scanned_replacement(tmp_path: Path) -> None:
    with session() as db:
        media = add_media(db, tmp_path / "library")
        original = Path(media.library.root_path) / media.relative_path
        item = service.move_to_trash(db, [media.id])[0]
        original.write_bytes(b"replacement")
        media.status = "active"
        media.size = original.stat().st_size
        media.mtime_ns = original.stat().st_mtime_ns
        db.commit()
        service.restore_from_trash(db, [item.id])
        assert media.relative_path == "photo.jpg" and media.status == "active"
        assert item.media_id != media.id
        assert original.read_bytes() == b"replacement"
        assert item.media.relative_path == "photo_restored_1.jpg"


def test_tampered_trash_is_never_deleted_or_restored(tmp_path: Path) -> None:
    with session() as db:
        media = add_media(db, tmp_path / "library")
        item = service.move_to_trash(db, [media.id])[0]
        source = get_settings().trash_dir / item.trash_relative_path
        source.write_bytes(b"tampered")
        with pytest.raises(HTTPException, match="校验失败"):
            service.permanently_delete(db, [item.id])
        with pytest.raises(HTTPException, match="校验失败"):
            service.restore_from_trash(db, [item.id])
        assert source.read_bytes() == b"tampered"


def test_readonly_library_cannot_move(tmp_path: Path) -> None:
    with session() as db:
        media = add_media(db, tmp_path / "library")
        media.library.writable = False
        db.commit()
        with pytest.raises(HTTPException, match="只读"):
            service.move_to_trash(db, [media.id])
        assert (tmp_path / "library" / "photo.jpg").exists()


def test_lazy_thumbnail_and_trash_thumbnail(tmp_path: Path) -> None:
    with session() as db:
        media = add_media(db, tmp_path / "library")
        source = Path(media.library.root_path) / media.relative_path
        Image.new("RGB", (800, 1200), "red").save(source)
        media.size = source.stat().st_size
        media.mtime_ns = source.stat().st_mtime_ns
        db.commit()
        assert not list(get_settings().cache_dir.rglob("*.webp"))
        cached = thumbnails.thumbnail_path(db, media, 256)
        with Image.open(cached) as image:
            assert max(image.size) == 256
            assert image.format == "WEBP"
        assert thumbnails.thumbnail_path(db, media, 256) == cached
        item = service.move_to_trash(db, [media.id])[0]
        trash_cached = thumbnails.trash_thumbnail_path(db, item, 256)
        assert trash_cached.exists() and trash_cached != cached
        service.permanently_delete(db, [item.id])
        with pytest.raises(FileNotFoundError):
            thumbnails.trash_thumbnail_path(db, item, 256)


def test_thumbnail_rejects_stale_media_and_cache_quota(tmp_path: Path) -> None:
    with session() as db:
        media = add_media(db, tmp_path / "library")
        source = Path(media.library.root_path) / media.relative_path
        source.write_bytes(b"new-content")
        with pytest.raises(ValueError, match="重新扫描"):
            thumbnails.thumbnail_path(db, media, 256)
    root = get_settings().cache_dir / "thumbnails"
    root.mkdir(parents=True)
    (root / "old.webp").write_bytes(b"thumbnail")
    (root / ".partial.tmp.webp").write_bytes(b"partial")
    assert thumbnails.enforce_cache_quota(0) == {"bytes": 0, "removed": 1}
    assert (root / ".partial.tmp.webp").exists()
