from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from app.database import Base
from app.models import Classification, DuplicateRun, Job, Library, MediaFile
from app.scanner import scan_library
from app.services.duplicates import run_duplicate_detection
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


@pytest.fixture(autouse=True)
def allowed_test_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "app.core.paths.get_settings", lambda: SimpleNamespace(allowed_media_roots=[tmp_path])
    )


def memory_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def make_png(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1170, 2532), color).save(path)


def test_incremental_scan_is_idempotent_and_marks_missing(tmp_path: Path) -> None:
    root = tmp_path / "media"
    make_png(root / "Screenshots" / "Screenshot_001.png", (10, 40, 80))
    with memory_session() as db:
        library = Library(name="照片", root_path=str(root), writable=True)
        db.add(library)
        db.flush()
        first = Job(
            kind="scan", status="running", payload={"library_id": library.id, "mode": "full"}
        )
        db.add(first)
        db.commit()
        result = scan_library(db, first)
        assert result["added"] == 1
        media = db.scalar(select(MediaFile))
        assert media is not None and media.width == 1170
        classification = db.scalar(select(Classification))
        assert classification is not None and classification.category == "screenshot"

        second = Job(
            kind="scan",
            status="running",
            payload={"library_id": library.id, "mode": "incremental"},
        )
        db.add(second)
        db.commit()
        result = scan_library(db, second)
        assert result["unchanged"] == 1
        assert db.query(MediaFile).count() == 1

        (root / "Screenshots" / "Screenshot_001.png").unlink()
        third = Job(
            kind="scan",
            status="running",
            payload={"library_id": library.id, "mode": "incremental"},
        )
        db.add(third)
        db.commit()
        scan_library(db, third)
        assert db.scalar(select(MediaFile)).status == "missing"


def test_exact_duplicates_require_sha256(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    make_png(root_a / "one.png", (120, 20, 40))
    root_b.mkdir()
    (root_b / "renamed.png").write_bytes((root_a / "one.png").read_bytes())
    make_png(root_b / "different.png", (20, 120, 40))
    with memory_session() as db:
        libraries = [
            Library(name="A", root_path=str(root_a), writable=True),
            Library(name="B", root_path=str(root_b), writable=True),
        ]
        db.add_all(libraries)
        db.commit()
        for library in libraries:
            job = Job(
                kind="scan",
                status="running",
                payload={"library_id": library.id, "mode": "full"},
            )
            db.add(job)
            db.commit()
            scan_library(db, job)
        job = Job(kind="duplicate", status="running", payload={})
        db.add(job)
        db.flush()
        run = DuplicateRun(
            job_id=job.id, library_ids=[item.id for item in libraries], media_type="photo"
        )
        db.add(run)
        db.flush()
        job.payload = {"duplicate_run_id": run.id}
        db.commit()
        result = run_duplicate_detection(db, job)
        assert result["groups"] == 1
