"""Create a disposable 100k-row SQLite database and measure the media list query."""

from __future__ import annotations

import argparse
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from app.database.session import Base
from app.models import Library, MediaFile
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session


def build_database(path: Path, rows: int) -> None:
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def configure(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        library = Library(
            name="Benchmark",
            root_path="/media/benchmark",
            enabled=True,
            writable=False,
            created_at=now,
        )
        db.add(library)
        db.flush()
        batch: list[MediaFile] = []
        for index in range(rows):
            batch.append(
                MediaFile(
                    library_id=library.id,
                    relative_path=f"{index // 1000:03d}/IMG_{index:06d}.jpg",
                    filename=f"IMG_{index:06d}.jpg",
                    extension=".jpg",
                    media_type="photo",
                    status="active",
                    size=1_000_000 + index,
                    mtime_ns=1_700_000_000_000_000_000 + index,
                    created_at=now,
                    updated_at=now,
                )
            )
            if len(batch) == 1_000:
                db.add_all(batch)
                db.commit()
                batch.clear()
        if batch:
            db.add_all(batch)
            db.commit()
    engine.dispose()


def measure(path: Path, iterations: int) -> list[float]:
    engine = create_engine(f"sqlite:///{path}")
    timings: list[float] = []
    with Session(engine) as db:
        statement = (
            select(MediaFile)
            .where(MediaFile.status == "active")
            .order_by(MediaFile.mtime_ns.desc(), MediaFile.id.desc())
            .limit(50)
        )
        db.scalars(statement).all()
        for _ in range(iterations):
            started = time.perf_counter()
            results = db.scalars(statement).all()
            elapsed_ms = (time.perf_counter() - started) * 1_000
            assert len(results) == 50
            timings.append(elapsed_ms)
    engine.dispose()
    return timings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--iterations", type=int, default=20)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="photosweep-benchmark-") as directory:
        database = Path(directory) / "benchmark.db"
        build_database(database, args.rows)
        timings = measure(database, args.iterations)
        timings_sorted = sorted(timings)
        p95_index = min(len(timings_sorted) - 1, round(len(timings_sorted) * 0.95) - 1)
        print(f"rows={args.rows}")
        print(f"iterations={args.iterations}")
        print(f"median_ms={statistics.median(timings):.2f}")
        print(f"p95_ms={timings_sorted[p95_index]:.2f}")
        print(f"max_ms={max(timings):.2f}")


if __name__ == "__main__":
    main()
