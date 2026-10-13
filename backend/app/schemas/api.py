from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SetupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[\w.-]+$")
    password: str = Field(min_length=12, max_length=256)


class OnboardingDismissRequest(BaseModel):
    dismissed: bool = True


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LibraryCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    root_path: str = Field(min_length=1, max_length=2048)


class LibraryUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None


class LibraryOut(OrmModel):
    id: int
    name: str
    root_path: str
    enabled: bool
    writable: bool
    last_scan_at: datetime | None


class ScanRequest(BaseModel):
    mode: Literal["full", "incremental"] = "incremental"


class DuplicateRunRequest(BaseModel):
    library_ids: list[int] = Field(min_length=1, max_length=100)
    media_type: Literal["photo", "video", "both"] = "both"

    @field_validator("library_ids")
    @classmethod
    def unique_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("Library 不能重复")
        return value


class BatchDecisionRequest(BaseModel):
    media_ids: list[int] = Field(min_length=1, max_length=500)
    category: Literal["screenshot", "screen_recording", "app_image"]
    decision: Literal["review", "keep"]


class BatchMediaRequest(BaseModel):
    media_ids: list[int] = Field(min_length=1, max_length=500)


class BatchTrashRequest(BaseModel):
    trash_item_ids: list[int] = Field(min_length=1, max_length=500)


class PermanentDeleteRequest(BatchTrashRequest):
    password: str = Field(min_length=1, max_length=256)


class KeepMemberRequest(BaseModel):
    media_id: int


class MediaOut(OrmModel):
    id: int
    library_id: int
    library_name: str
    library_writable: bool
    relative_path: str
    filename: str
    extension: str
    media_type: str
    status: str
    size: int
    mtime_ns: int
    captured_at: datetime | None
    width: int | None
    height: int | None
    duration_ms: int | None
    fps: float | None
    codec: str | None
    camera_make: str | None
    camera_model: str | None
    metadata_error: str | None


class SettingsUpdate(BaseModel):
    thumbnail_cache_gb: int | None = Field(default=None, ge=1, le=100)
    metadata_workers: int | None = Field(default=None, ge=1, le=8)
