from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PHOTOSWEEP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "PhotoSweep"
    data_dir: Path = Path("./data")
    cache_dir: Path = Path("./cache")
    trash_dir: Path = Path("./trash")
    allowed_media_roots: Annotated[list[Path], NoDecode] = Field(
        default_factory=lambda: [Path("/media")]
    )
    cookie_secure: bool = False
    public_origin: str = ""
    session_days: int = Field(default=14, ge=1, le=365)
    job_workers: int = Field(default=1, ge=1, le=1)
    metadata_workers: int = Field(default=2, ge=1, le=8)
    frontend_dist: Path | None = None

    @field_validator("public_origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("PUBLIC_ORIGIN 必须是完整 HTTP(S) 源，不能含路径或密码")
        return value

    @field_validator("allowed_media_roots", mode="before")
    @classmethod
    def split_roots(cls, value: object) -> object:
        if isinstance(value, str):
            return [Path(item.strip()) for item in value.split(",") if item.strip()]
        return value

    def prepare_directories(self) -> None:
        for path in (self.data_dir, self.cache_dir, self.trash_dir):
            path.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "thumbnails").mkdir(parents=True, exist_ok=True)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{(self.data_dir / 'photosweep.db').resolve().as_posix()}"

    @property
    def resolved_frontend_dist(self) -> Path:
        if self.frontend_dist:
            return self.frontend_dist.resolve()
        return (Path(__file__).resolve().parents[3] / "frontend" / "dist" / "client").resolve()


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare_directories()
    return settings
