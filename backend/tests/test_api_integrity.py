from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from app.core.config import get_settings
from app.core.security import LoginLimiter, login_limiter, reauth_limiter
from app.database import Base, SessionLocal, init_db
from app.main import app
from app.models import (
    Classification,
    DuplicateGroup,
    DuplicateMember,
    DuplicateRun,
    Job,
    Library,
    MediaFile,
    SessionToken,
    TrashItem,
)
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, select


@pytest.fixture
def client():
    init_db()
    with SessionLocal() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(delete(table))
        db.commit()
    login_limiter._records.clear()
    reauth_limiter._records.clear()
    with TestClient(app) as browser:
        response = browser.post(
            "/api/v1/setup/admin", json={"username": "admin", "password": "correct-horse-battery"}
        )
        assert response.status_code == 201, response.text
        browser.headers["X-CSRF-Token"] = browser.cookies["photosweep_csrf"]
        yield browser
    with SessionLocal() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(delete(table))
        db.commit()


def seed_media(db, name: str, count=1):
    root = get_settings().allowed_media_roots[0] / name
    root.mkdir(parents=True, exist_ok=True)
    library = Library(name=name, root_path=str(root), writable=True)
    db.add(library)
    db.flush()
    files = []
    for index in range(count):
        path = root / f"{index}.jpg"
        path.write_bytes(b"fixture")
        st = path.stat()
        media = MediaFile(
            library_id=library.id,
            relative_path=path.name,
            filename=path.name,
            extension=".jpg",
            media_type="photo",
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            status="active",
        )
        db.add(media)
        files.append(media)
    db.flush()
    return library, files


def test_validation_problem_and_cookie_security(client):
    response = client.post("/api/v1/duplicate-runs", json={"library_ids": [1, 1]})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "password" not in response.text
    assert (
        client.post(
            "/api/v1/setup/admin", json={"username": "other", "password": "correct-horse-battery"}
        ).status_code
        == 409
    )
    login = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery"}
    )
    cookies = login.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("photosweep_session="))
    assert "HttpOnly" in session_cookie and "SameSite=strict" in session_cookie
    assert (
        client.post(
            "/api/v1/setup/onboarding/dismiss", headers={"Origin": "https://evil.example"}, json={}
        ).status_code
        == 403
    )
    assert client.get("/api/v1/not-a-real-route").status_code == 404


def test_limiter_reserves_concurrent_attempts():
    limiter = LoginLimiter(attempts=5)
    barrier = threading.Barrier(16)

    def attempt(_):
        barrier.wait()
        try:
            limiter.check("same-client")
            return True
        except HTTPException as exc:
            assert exc.status_code == 429
            return False

    with ThreadPoolExecutor(max_workers=16) as workers:
        assert sum(workers.map(attempt, range(16))) == 5


def test_expired_session_and_private_thumbnail(client):
    with SessionLocal() as db:
        session = db.scalar(select(SessionToken))
        session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert client.get("/api/v1/media/1/thumbnail").status_code == 401
    assert client.get("/api/v1/setup/directories").status_code == 401


def test_summary_scope_keep_fingerprint_and_page_bytes(client):
    with SessionLocal() as db:
        a, first = seed_media(db, "scope-a", 2)
        b, second = seed_media(db, "scope-b")
        for media in first + second:
            db.add(
                Classification(
                    media_id=media.id,
                    category="screenshot",
                    confidence=90,
                    reasons=["test"],
                    detector_version="test",
                    fingerprint=f"{media.size}:{media.mtime_ns}",
                )
            )
        db.commit()
        aid, bid, media_id = a.id, b.id, first[0].id
        changed_path = get_settings().allowed_media_roots[0] / "scope-a" / first[0].filename
    summary = client.get("/api/v1/dashboard/summary", params={"library_ids": aid}).json()["data"]
    assert summary["photos"]["count"] == summary["screenshot"]["count"] == 2
    page = client.get("/api/v1/media", params={"library_ids": bid, "category": "screenshot"}).json()
    assert page["meta"]["total"] == 1 and page["meta"]["total_bytes"] == 7
    assert (
        client.post(
            "/api/v1/media/batch-decision",
            json={"media_ids": [media_id], "category": "screenshot", "decision": "keep"},
        ).status_code
        == 200
    )
    kept = client.get("/api/v1/media", params={"category": "screenshot", "decision": "keep"}).json()
    assert kept["meta"]["total"] == 1
    changed_path.write_bytes(b"changed on disk")
    assert (
        client.post(
            "/api/v1/media/batch-decision",
            json={"media_ids": [media_id], "category": "screenshot", "decision": "review"},
        ).status_code
        == 409
    )


def test_duplicate_members_are_bounded_and_keep_survives_paging(client):
    with SessionLocal() as db:
        library, media = seed_media(db, "many-copies", 105)
        job = Job(kind="duplicate", status="completed")
        db.add(job)
        db.flush()
        run = DuplicateRun(job_id=job.id, library_ids=[library.id], media_type="photo")
        db.add(run)
        db.flush()
        group = DuplicateGroup(
            run_id=run.id,
            media_type="photo",
            size=7,
            quick_hash="x",
            sha256="f" * 64,
            copy_count=105,
            potential_savings=728,
        )
        db.add(group)
        db.flush()
        for index, item in enumerate(media):
            db.add(
                DuplicateMember(
                    group_id=group.id,
                    media_id=item.id,
                    is_keep=index == 0,
                    is_default_keep=index == 0,
                )
            )
        db.commit()
        group_id, run_id, last_id = group.id, run.id, media[-1].id
    groups = client.get("/api/v1/duplicate-groups", params={"run_id": run_id}).json()
    assert len(groups["data"][0]["members"]) == 50
    assert groups["data"][0]["members_total"] == 105
    assert groups["data"][0]["members_has_more"] is True
    members = client.get(f"/api/v1/duplicate-groups/{group_id}/members", params={"page": 3}).json()
    assert len(members["data"]) == 5 and members["meta"]["pages"] == 3
    assert (
        client.put(
            f"/api/v1/duplicate-groups/{group_id}/keep", json={"media_id": last_id}
        ).status_code
        == 200
    )
    first_page = client.get(f"/api/v1/duplicate-groups/{group_id}/members").json()["data"]
    assert first_page[0]["id"] == last_id and first_page[0]["is_keep"]
    assert not first_page[0]["is_default_keep"]
    with SessionLocal() as db:
        run = db.get(DuplicateRun, run_id)
        job = db.get(Job, run.job_id)
        job.status = "interrupted"
        db.commit()
    assert (
        client.put(
            f"/api/v1/duplicate-groups/{group_id}/keep", json={"media_id": last_id}
        ).status_code
        == 409
    )
    with SessionLocal() as db:
        job = db.get(Job, db.get(DuplicateRun, run_id).job_id)
        job.status = "completed"
        db.commit()
        media = db.get(MediaFile, last_id)
        path = get_settings().allowed_media_roots[0] / "many-copies" / media.relative_path
        path.write_bytes(b"changed")
        import os

        os.utime(path, ns=(1, 1))
    assert (
        client.put(
            f"/api/v1/duplicate-groups/{group_id}/keep", json={"media_id": last_id}
        ).status_code
        == 409
    )
    assert client.get(f"/api/v1/duplicate-groups/{group_id}/members").status_code == 404


def test_https_proxy_origin_is_explicit_and_cross_site_rejected(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "public_origin", "https://photos.example.test")
    response = client.post(
        "/api/v1/setup/onboarding/dismiss",
        headers={"Origin": "https://photos.example.test"},
        json={},
    )
    assert response.status_code == 200
    response = client.post(
        "/api/v1/setup/onboarding/dismiss",
        headers={"Origin": "https://other.example.test"},
        json={},
    )
    assert response.status_code == 403


def test_sse_snapshot_and_terminal_event(client):
    with SessionLocal() as db:
        job = Job(kind="scan", status="completed", progress_seq=4)
        db.add(job)
        db.commit()
        job_id = job.id
    response = client.get(f"/api/v1/events/jobs/{job_id}")
    assert response.text.startswith("event: job.snapshot\n")
    assert "event: job.completed\n" in response.text
    assert "+00:00" in response.text
    assert client.get("/api/v1/events/jobs/9999999").status_code == 404


def test_library_delete_only_database_history(client):
    with SessionLocal() as db:
        library, media = seed_media(db, "delete-history")
        source = get_settings().allowed_media_roots[0] / "delete-history" / "0.jpg"
        item = TrashItem(
            item_uuid="history",
            library_id=library.id,
            media_id=media[0].id,
            original_relative_path="0.jpg",
            trash_relative_path="history/0.jpg",
            filename="0.jpg",
            size=7,
            mtime_ns=1,
            sha256="f" * 64,
            status="restored",
        )
        db.add(item)
        db.commit()
        library_id = library.id
    response = client.delete(f"/api/v1/libraries/{library_id}")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["files_deleted"] is False
    assert source.read_bytes() == b"fixture"


def test_bad_password_reauth_is_limited_without_logout(client):
    for _ in range(5):
        assert (
            client.post(
                "/api/v1/trash/actions/permanent-delete",
                json={"trash_item_ids": [1], "password": "incorrect"},
            ).status_code
            == 403
        )
    assert (
        client.post(
            "/api/v1/trash/actions/permanent-delete",
            json={"trash_item_ids": [1], "password": "incorrect"},
        ).status_code
        == 429
    )
    assert client.get("/api/v1/auth/me").status_code == 200
