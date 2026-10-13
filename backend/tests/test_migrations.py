from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("mode", ["legacy", "versioned", "interrupted"])
def test_database_upgrade_preserves_data_and_recovers_partial_migration(tmp_path, mode):
    repo = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    for name in ("data", "cache", "trash"):
        environment[f"PHOTOSWEEP_{name.upper()}_DIR"] = str(tmp_path / name)
    environment["PYTHONPATH"] = str(repo / "backend")
    code = """
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from app.database.session import engine, init_db
from app.core.config import get_settings
config = Config("alembic.ini")
command.upgrade(config, "19382d62dd81")
with engine.begin() as connection:
    connection.exec_driver_sql(
        "INSERT INTO admin_accounts (id,username,password_hash,password_changed_at,created_at) "
        "VALUES (1,'preserved','not-a-real-password',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
    if MODE == "legacy":
        connection.exec_driver_sql("DROP TABLE alembic_version")
    if MODE == "interrupted":
        connection.exec_driver_sql("ALTER TABLE duplicate_members "
            "ADD COLUMN is_default_keep BOOLEAN NOT NULL DEFAULT 0")
        connection.exec_driver_sql("CREATE INDEX ix_media_status_mtime "
            "ON media_files (status,mtime_ns,id)")
init_db()
init_db()
with engine.connect() as connection:
    assert connection.exec_driver_sql("SELECT username FROM admin_accounts").scalar() == "preserved"
    revision = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    assert revision == "20261013_review_integrity"
columns = {c["name"] for c in inspect(engine).get_columns("duplicate_members")}
assert "is_default_keep" in columns
indexes = {i["name"] for i in inspect(engine).get_indexes("media_files")}
assert {"ix_media_status_mtime", "ix_media_library_status_mtime"}.issubset(indexes)
assert list(get_settings().data_dir.glob("pre-upgrade-*.db"))
engine.dispose()
"""
    result = subprocess.run(
        [sys.executable, "-c", f"MODE={mode!r}\n" + code],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
