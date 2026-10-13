from __future__ import annotations

import threading
import time

import pytest
from app.database import Base
from app.jobs import runner
from app.models import Job
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'jobs.sqlite').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(runner, "SessionLocal", factory)
    yield factory
    engine.dispose()


def test_restart_interrupts_running_and_executes_persisted_queued(sessions, monkeypatch):
    with sessions() as db:
        running = Job(kind="scan", status="running")
        queued = Job(kind="scan", status="queued")
        db.add_all([running, queued])
        db.commit()
        running_id, queued_id = running.id, queued.id
    called = threading.Event()

    def complete(db, job):
        called.set()
        return {"processed": 1}

    monkeypatch.setattr(runner, "scan_library", complete)
    worker = runner.JobRunner()
    try:
        worker.start()
        assert called.wait(timeout=3)
        for _ in range(100):
            with sessions() as db:
                if db.get(Job, queued_id).status == "completed":
                    break
            time.sleep(0.01)
        with sessions() as db:
            assert db.get(Job, running_id).status == "interrupted"
            assert db.get(Job, running_id).progress_seq == 1
            assert db.get(Job, queued_id).status == "completed"
    finally:
        worker.stop()


def test_cancelled_queued_job_never_invokes_handler(sessions, monkeypatch):
    with sessions() as db:
        job = Job(kind="scan", cancel_requested=True)
        db.add(job)
        db.commit()
        job_id = job.id
    calls = []
    monkeypatch.setattr(runner, "scan_library", lambda db, job: calls.append(job.id))
    runner.JobRunner()._execute(job_id)
    with sessions() as db:
        assert db.get(Job, job_id).status == "cancelled"
    assert not calls


def test_repeated_start_does_not_interrupt_live_worker(sessions, monkeypatch):
    started, finish = threading.Event(), threading.Event()

    def handler(db, job):
        started.set()
        assert finish.wait(timeout=3)
        return {}

    monkeypatch.setattr(runner, "scan_library", handler)
    with sessions() as db:
        db.add(Job(kind="scan"))
        db.commit()
    worker = runner.JobRunner()
    try:
        worker.start()
        assert started.wait(timeout=3)
        worker.start()
        with sessions() as db:
            assert db.scalar(select(Job.status)) == "running"
    finally:
        finish.set()
        worker.stop()
