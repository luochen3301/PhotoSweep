from __future__ import annotations

from pathlib import Path

import pytest
from app.core.config import get_settings
from app.database import Base
from app.file_actions import move_to_trash, permanently_delete, restore_from_trash
from app.models import Library, MediaFile
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def add_media(db: Session, root: Path, name: str = "photo.jpg") -> MediaFile:
    source = root / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"safe-photo-content")
    stat = source.stat()
    library = Library(name=f"Library-{name}", root_path=str(root), writable=True)
    db.add(library)
    db.flush()
    media = MediaFile(
        library_id=library.id,
        relative_path=name,
        filename=name,
        extension=".jpg",
        media_type="photo",
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        status="active",
    )
    db.add(media)
    db.commit()
    return media


def test_changed_file_is_never_moved(tmp_path: Path) -> None:
    settings = get_settings()
    settings.trash_dir = tmp_path / "trash"
    settings.trash_dir.mkdir()
    with session() as db:
        media = add_media(db, tmp_path / "library")
        source = Path(media.library.root_path) / media.relative_path
        source.write_bytes(b"changed-after-scan")
        with pytest.raises(HTTPException, match="File changed since scan"):
            move_to_trash(db, [media.id])
        assert source.exists()


def test_move_restore_and_permanent_delete(tmp_path: Path) -> None:
    settings = get_settings()
    settings.trash_dir = tmp_path / "trash"
    settings.trash_dir.mkdir()
    with session() as db:
        media = add_media(db, tmp_path / "library", "Screenshot.png")
        original = Path(media.library.root_path) / media.relative_path
        item = move_to_trash(db, [media.id])[0]
        assert not original.exists()
        assert item.status == "active"
        restored = restore_from_trash(db, [item.id])[0]
        assert restored.status == "restored"
        assert original.exists()

        second = add_media(db, tmp_path / "library-2", "delete-me.jpg")
        second_item = move_to_trash(db, [second.id])[0]
        assert permanently_delete(db, [second_item.id]) == 1
        assert second_item.status == "deleted"
