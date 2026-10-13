from __future__ import annotations

import sqlite3
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    get_settings().database_url,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@event.listens_for(Engine, "connect")
def configure_sqlite(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def init_db() -> None:
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app import models  # noqa: F401

    repo = Path(__file__).resolve().parents[3]
    config = Config(str(repo / "alembic.ini"))
    config.set_main_option("script_location", str(repo / "backend" / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    legacy = "admin_accounts" in tables and "alembic_version" not in tables
    if legacy:
        # Old local-development instances used create_all. Only adopt recognized schemas.
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                raise RuntimeError("无法识别旧数据库结构，请保留数据并从备份恢复")
            columns = {column["name"] for column in inspector.get_columns(table.name)}
            expected = set(table.columns.keys()) - {"is_default_keep"}
            if not expected.issubset(columns):
                raise RuntimeError("旧数据库缺少必要字段，已停止升级以保护数据")
    current = None
    if "alembic_version" in tables:
        with engine.connect() as connection:
            current = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    if tables and current != head:
        backup_path = get_settings().data_dir / (
            "pre-upgrade-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".db"
        )
        with engine.connect() as source, sqlite3.connect(backup_path) as destination:
            source.connection.driver_connection.backup(destination)
    if legacy:
        command.stamp(config, "19382d62dd81")
    command.upgrade(config, "head")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
