from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MediaType(str, enum.Enum):
    PHOTO = "photo"
    VIDEO = "video"


class MediaStatus(str, enum.Enum):
    ACTIVE = "active"
    MISSING = "missing"
    TRASHED = "trashed"
    ERROR = "error"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class AdminAccount(Base):
    __tablename__ = "admin_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SessionToken(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("admin_accounts.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    admin: Mapped[AdminAccount] = relationship()


class Library(Base):
    __tablename__ = "libraries"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    root_path: Mapped[str] = mapped_column(Text, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    writable: Mapped[bool] = mapped_column(Boolean, default=False)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    media_files: Mapped[list["MediaFile"]] = relationship(
        back_populates="library", cascade="all, delete-orphan"
    )


class MediaFile(Base):
    __tablename__ = "media_files"
    __table_args__ = (
        UniqueConstraint("library_id", "relative_path", name="uq_media_library_path"),
        Index("ix_media_status_type", "status", "media_type"),
        Index("ix_media_size_type_status", "size", "media_type", "status"),
        Index("ix_media_mtime", "mtime_ns", "id"),
        Index("ix_media_status_mtime", "status", "mtime_ns", "id"),
        Index("ix_media_library_status_mtime", "library_id", "status", "mtime_ns", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    relative_path: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    extension: Mapped[str] = mapped_column(String(16))
    media_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=MediaStatus.ACTIVE.value)
    size: Mapped[int] = mapped_column(Integer)
    mtime_ns: Mapped[int] = mapped_column(Integer)
    filesystem_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    codec: Mapped[str | None] = mapped_column(String(80))
    camera_make: Mapped[str | None] = mapped_column(String(120))
    camera_model: Mapped[str | None] = mapped_column(String(120))
    file_device: Mapped[int | None] = mapped_column(Integer)
    file_inode: Mapped[int | None] = mapped_column(Integer)
    quick_hash: Mapped[str | None] = mapped_column(String(32))
    quick_hash_version: Mapped[str | None] = mapped_column(String(32))
    sha256: Mapped[str | None] = mapped_column(String(64))
    metadata_error: Mapped[str | None] = mapped_column(Text)
    last_seen_job_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    library: Mapped[Library] = relationship(back_populates="media_files")
    classifications: Mapped[list["Classification"]] = relationship(
        back_populates="media", cascade="all, delete-orphan"
    )


class Classification(Base):
    __tablename__ = "classifications"
    __table_args__ = (
        UniqueConstraint("media_id", "category", name="uq_classification_media_category"),
        Index("ix_classification_queue", "category", "decision", "confidence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media_files.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(40))
    app_source: Mapped[str | None] = mapped_column(String(40))
    confidence: Mapped[int] = mapped_column(Integer)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    detector_version: Mapped[str] = mapped_column(String(20))
    decision: Mapped[str] = mapped_column(String(16), default="review")
    fingerprint: Mapped[str] = mapped_column(String(80))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    media: Mapped[MediaFile] = relationship(back_populates="classifications")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED.value)
    phase: Mapped[str] = mapped_column(String(80), default="等待执行")
    total: Mapped[int | None] = mapped_column(Integer)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    progress_seq: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DuplicateRun(Base):
    __tablename__ = "duplicate_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True)
    library_ids: Mapped[list[int]] = mapped_column(JSON)
    media_type: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    groups: Mapped[list["DuplicateGroup"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class DuplicateGroup(Base):
    __tablename__ = "duplicate_groups"
    __table_args__ = (Index("ix_duplicate_groups_run_type", "run_id", "media_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("duplicate_runs.id", ondelete="CASCADE"))
    media_type: Mapped[str] = mapped_column(String(16))
    size: Mapped[int] = mapped_column(Integer)
    quick_hash: Mapped[str] = mapped_column(String(32))
    sha256: Mapped[str] = mapped_column(String(64))
    copy_count: Mapped[int] = mapped_column(Integer)
    potential_savings: Mapped[int] = mapped_column(Integer)
    run: Mapped[DuplicateRun] = relationship(back_populates="groups")
    members: Mapped[list["DuplicateMember"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class DuplicateMember(Base):
    __tablename__ = "duplicate_members"
    __table_args__ = (
        UniqueConstraint("group_id", "media_id", name="uq_duplicate_group_media"),
        Index("ix_duplicate_member_media", "media_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("duplicate_groups.id", ondelete="CASCADE"))
    media_id: Mapped[int] = mapped_column(ForeignKey("media_files.id", ondelete="CASCADE"))
    is_keep: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default_keep: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_hardlink: Mapped[bool] = mapped_column(Boolean, default=False)
    group: Mapped[DuplicateGroup] = relationship(back_populates="members")
    media: Mapped[MediaFile] = relationship()


class TrashItem(Base):
    __tablename__ = "trash_items"
    __table_args__ = (Index("ix_trash_status_moved", "status", "moved_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    item_uuid: Mapped[str] = mapped_column(String(36), unique=True)
    media_id: Mapped[int | None] = mapped_column(ForeignKey("media_files.id", ondelete="SET NULL"))
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id", ondelete="RESTRICT"))
    original_relative_path: Mapped[str] = mapped_column(Text)
    trash_relative_path: Mapped[str] = mapped_column(Text, unique=True)
    filename: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer)
    mtime_ns: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    category_snapshot: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="active")
    moved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    media: Mapped[MediaFile | None] = relationship()
    library: Mapped[Library] = relationship()


class FileOperation(Base):
    __tablename__ = "file_operations"
    __table_args__ = (Index("ix_file_operation_state", "state", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    operation: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(24), default="pending")
    media_id: Mapped[int | None] = mapped_column(Integer)
    trash_item_id: Mapped[int | None] = mapped_column(Integer)
    source_path: Mapped[str] = mapped_column(Text)
    destination_path: Mapped[str | None] = mapped_column(Text)
    expected_size: Mapped[int] = mapped_column(Integer)
    expected_mtime_ns: Mapped[int] = mapped_column(Integer)
    expected_sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
