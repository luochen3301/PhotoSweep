from __future__ import annotations

import queue
import threading
from datetime import datetime, timezone

from sqlalchemy import update

from app.database import SessionLocal
from app.models import Job
from app.scanner import scan_library
from app.services.duplicates import run_duplicate_detection


class JobRunner:
    def __init__(self) -> None:
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        with SessionLocal() as db:
            db.execute(
                update(Job)
                .where(Job.status == "running")
                .values(
                    status="interrupted",
                    phase="容器重启导致任务中断",
                    finished_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="photosweep-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=5)

    def enqueue(self, job_id: int) -> None:
        self._queue.put(job_id)

    def _run(self) -> None:
        while not self._stop.is_set():
            job_id = self._queue.get()
            if job_id is None:
                continue
            self._execute(job_id)

    def _execute(self, job_id: int) -> None:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if job is None or job.status != "queued":
                return
            job.status = "running"
            job.started_at = datetime.now(timezone.utc)
            job.heartbeat_at = datetime.now(timezone.utc)
            job.progress_seq += 1
            db.commit()
            try:
                if job.kind == "scan":
                    result = scan_library(db, job)
                elif job.kind == "duplicate":
                    result = run_duplicate_detection(db, job)
                else:
                    raise RuntimeError(f"未知任务类型: {job.kind}")
                db.refresh(job)
                if result.get("cancelled") or job.cancel_requested:
                    job.status = "cancelled"
                    job.phase = "已取消"
                else:
                    job.status = "completed"
                    job.phase = "已完成"
                job.result = result
            except Exception as exc:
                db.rollback()
                job = db.get(Job, job_id)
                if job:
                    job.status = "failed"
                    job.phase = "执行失败"
                    job.error = str(exc)[:4000]
            if job:
                job.progress_seq += 1
                job.finished_at = datetime.now(timezone.utc)
                db.commit()


job_runner = JobRunner()
