from __future__ import annotations

from app.main import app
from fastapi.testclient import TestClient


def test_setup_login_and_csrf_guard() -> None:
    with TestClient(app) as client:
        status = client.get("/api/v1/setup/status")
        assert status.status_code == 200
        if not status.json()["data"]["configured"]:
            response = client.post(
                "/api/v1/setup/admin",
                json={"username": "admin", "password": "correct-horse-battery"},
            )
            assert response.status_code == 201
        assert client.get("/api/v1/auth/me").status_code == 200
        assert client.post("/api/v1/auth/logout").status_code == 403
        csrf = client.cookies.get("photosweep_csrf")
        assert csrf
        assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
