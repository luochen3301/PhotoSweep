"""Authenticated API benchmark; isolated DB, including counts, joins and JSON serialization."""

import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="photosweep-api-benchmark-") as directory:
    root = Path(directory)
    for name in ("data", "cache", "trash"):
        os.environ[f"PHOTOSWEEP_{name.upper()}_DIR"] = str(root / name)
    os.environ["PHOTOSWEEP_ALLOWED_MEDIA_ROOTS"] = str(root / "media")
    os.environ["PHOTOSWEEP_COOKIE_SECURE"] = "false"
    (root / "media").mkdir()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from fastapi.testclient import TestClient
    from sqlalchemy import insert
    from app.database import SessionLocal
    from app.main import app
    from app.models import Classification, Library, MediaFile

    with TestClient(app) as client:
        assert (
            client.post(
                "/api/v1/setup/admin",
                json={"username": "benchmark", "password": "benchmark-only-password"},
            ).status_code
            == 201
        )
        with SessionLocal() as db:
            library = Library(name="Benchmark", root_path=str(root / "media"))
            db.add(library)
            db.flush()
            library_id = library.id
            for start in range(0, 100_000, 1000):
                rows = [
                    {
                        "id": i + 1,
                        "library_id": library_id,
                        "relative_path": f"{i}.jpg",
                        "filename": f"{i}.jpg",
                        "extension": ".jpg",
                        "media_type": "photo",
                        "status": "active",
                        "size": 1_000_000 + i,
                        "mtime_ns": 1_700_000_000_000_000_000 + i,
                    }
                    for i in range(start, start + 1000)
                ]
                db.execute(insert(MediaFile), rows)
                db.execute(
                    insert(Classification),
                    [
                        {
                            "media_id": row["id"],
                            "category": "screenshot",
                            "confidence": 90,
                            "reasons": ["benchmark"],
                            "detector_version": "test",
                            "decision": "review",
                            "fingerprint": "test",
                        }
                        for row in rows
                        if row["id"] % 5 == 0
                    ],
                )
                db.commit()
        reports = []
        for params in ({}, {"category": "screenshot"}, {"library_ids": library_id}):
            timings = []
            for _ in range(20):
                started = time.perf_counter()
                response = client.get("/api/v1/media", params=params)
                assert response.status_code == 200, response.text
                payload = response.json()
                assert len(payload["data"]) == 50 and payload["meta"]["total_bytes"] > 0
                timings.append((time.perf_counter() - started) * 1000)
            reports.append(
                {
                    "filter": params,
                    "rows": 100_000,
                    "median_ms": round(statistics.median(timings), 2),
                    "p95_ms": round(sorted(timings)[18], 2),
                    "max_ms": round(max(timings), 2),
                }
            )
        print(json.dumps(reports, indent=2))
        from app.database.session import engine

        engine.dispose()
        if any(report["p95_ms"] >= 500 for report in reports):
            raise SystemExit("API p95 exceeds the 500ms development-machine target")
