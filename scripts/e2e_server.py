"""Isolated real backend and real photos for Playwright. Never uses user media/data."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from PIL import Image

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "backend"))


def main():
    with tempfile.TemporaryDirectory(prefix="photosweep-e2e-") as directory:
        root = Path(directory)
        for name in ("data", "cache", "trash", "media"):
            os.environ[f"PHOTOSWEEP_{name.upper()}_DIR"] = str(root / name)
        os.environ["PHOTOSWEEP_ALLOWED_MEDIA_ROOTS"] = str(root / "media")
        os.environ["PHOTOSWEEP_FRONTEND_DIST"] = str(repo / "frontend" / "dist" / "client")
        os.environ["PHOTOSWEEP_COOKIE_SECURE"] = "false"
        personal = root / "media" / "personal"
        personal.mkdir(parents=True)
        image = Image.new("RGB", (1080, 1920), "#254c64")
        image.save(personal / "Screenshot_2026_01.png")
        image.save(personal / "Screenshot_2026_02.png")
        Image.new("RGB", (720, 720), "#71415e").save(personal / "mmexport1723456789012.jpg")
        import uvicorn

        uvicorn.run("app.main:app", host="127.0.0.1", port=8765, log_level="warning")
        from app.database.session import engine

        engine.dispose()


if __name__ == "__main__":
    main()
