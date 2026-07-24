from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.core.config import get_settings
from app.core.paths import list_media_directories
from app.database.session import SessionLocal
from app.main import app
from app.models import Library, Setting
from fastapi.testclient import TestClient
from sqlalchemy import delete


def _authenticated_client() -> TestClient:
    client = TestClient(app)
    client.__enter__()
    status = client.get("/api/v1/setup/status").json()["data"]
    if status["configured"]:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "correct-horse-battery"},
        )
        assert response.status_code == 200
    else:
        response = client.post(
            "/api/v1/setup/admin",
            json={"username": "admin", "password": "correct-horse-battery"},
        )
        assert response.status_code == 201
    return client


def _clear_onboarding_state() -> None:
    with SessionLocal() as db:
        db.execute(delete(Library))
        db.execute(delete(Setting).where(Setting.key == "onboarding_dismissed"))
        db.commit()


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get("photosweep_csrf")
    assert token
    return {"X-CSRF-Token": token}


def test_onboarding_requires_authentication() -> None:
    with TestClient(app) as client:
        assert client.get("/api/v1/setup/onboarding").status_code == 401
        assert client.get("/api/v1/setup/directories").status_code == 401


def test_directory_browser_dismiss_and_library_completion() -> None:
    _clear_onboarding_state()
    media_root = get_settings().allowed_media_roots[0]
    personal = media_root / "personal"
    nested = personal / "album"
    nested.mkdir(parents=True, exist_ok=True)
    (media_root / "loose-file.jpg").write_bytes(b"not-an-image")

    client = _authenticated_client()
    try:
        onboarding = client.get("/api/v1/setup/onboarding")
        assert onboarding.status_code == 200
        assert onboarding.json()["data"]["has_library"] is False
        assert onboarding.json()["data"]["dismissed"] is False
        assert onboarding.json()["data"]["media_available"] is True

        roots = client.get("/api/v1/setup/directories")
        assert roots.status_code == 200
        assert roots.json()["data"]["entries"][0]["path"] == str(media_root.resolve())

        listing = client.get(
            "/api/v1/setup/directories", params={"path": str(media_root.resolve())}
        )
        assert listing.status_code == 200
        names = [item["name"] for item in listing.json()["data"]["entries"]]
        assert "personal" in names
        assert "loose-file.jpg" not in names

        nested_listing = client.get(
            "/api/v1/setup/directories", params={"path": str(personal.resolve())}
        )
        assert nested_listing.status_code == 200
        assert nested_listing.json()["data"]["parent_path"] == str(media_root.resolve())
        assert nested_listing.json()["data"]["entries"][0]["name"] == "album"

        outside = client.get(
            "/api/v1/setup/directories", params={"path": str(media_root.parent)}
        )
        assert outside.status_code == 400

        assert client.post("/api/v1/setup/onboarding/dismiss").status_code == 403
        dismissed = client.post(
            "/api/v1/setup/onboarding/dismiss",
            json={"dismissed": True},
            headers=_csrf(client),
        )
        assert dismissed.status_code == 200
        assert client.get("/api/v1/setup/onboarding").json()["data"]["dismissed"] is True

        created = client.post(
            "/api/v1/libraries",
            json={"name": "Onboarding Test", "root_path": str(personal.resolve())},
            headers=_csrf(client),
        )
        assert created.status_code == 201
        assert created.json()["data"]["root_path"] == str(personal.resolve())
        completed = client.get("/api/v1/setup/onboarding").json()["data"]
        assert completed["has_library"] is True
        assert completed["library_count"] == 1
    finally:
        client.__exit__(None, None, None)
        _clear_onboarding_state()


def test_directory_browser_marks_permission_states(monkeypatch: pytest.MonkeyPatch) -> None:
    media_root = get_settings().allowed_media_roots[0]
    readable = media_root / "readable"
    locked = media_root / "locked"
    readable.mkdir(parents=True, exist_ok=True)
    locked.mkdir(parents=True, exist_ok=True)
    real_access = os.access

    def fake_access(path: os.PathLike[str] | str, mode: int) -> bool:
        candidate = Path(path)
        if candidate == locked:
            return False
        if candidate == readable and mode == os.W_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr("app.core.paths.os.access", fake_access)
    listing = list_media_directories(str(media_root.resolve()))
    by_name = {item["name"]: item for item in listing["entries"]}
    assert by_name["locked"]["readable"] is False
    assert by_name["locked"]["selectable"] is False
    assert by_name["readable"]["readable"] is True
    assert by_name["readable"]["writable"] is False


def test_directory_symlink_is_visible_but_never_browsable() -> None:
    media_root = get_settings().allowed_media_roots[0]
    target = media_root / "symlink-target"
    link = media_root / "symlink-link"
    target.mkdir(parents=True, exist_ok=True)
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("当前系统不允许测试进程创建目录符号链接")

    client = _authenticated_client()
    try:
        listing = client.get(
            "/api/v1/setup/directories", params={"path": str(media_root.resolve())}
        )
        member = next(
            item for item in listing.json()["data"]["entries"] if item["name"] == link.name
        )
        assert member["is_symlink"] is True
        assert member["selectable"] is False
        assert (
            client.get("/api/v1/setup/directories", params={"path": str(link)}).status_code
            == 409
        )
    finally:
        client.__exit__(None, None, None)
        link.unlink(missing_ok=True)
