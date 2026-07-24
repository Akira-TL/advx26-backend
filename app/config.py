from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Settings:
    base_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1])
    storage_dir: Path | None = None
    database_path: Path | None = None
    object_store_dir: Path | None = None
    object_staging_dir: Path | None = None
    api_token: str = field(default_factory=lambda: os.getenv("BACKEND_API_TOKEN", ""))
    trigger_token: str = field(
        default_factory=lambda: os.getenv("BACKEND_TRIGGER_TOKEN", "")
    )
    playback_token: str = field(
        default_factory=lambda: os.getenv("BACKEND_PLAYBACK_TOKEN", "")
    )
    cors_origins: str = field(default_factory=lambda: os.getenv("BACKEND_CORS_ORIGINS", "*"))
    ffmpeg_binary: str = field(
        default_factory=lambda: os.getenv("BACKEND_FFMPEG_BINARY", "ffmpeg")
    )
    ffprobe_binary: str = field(
        default_factory=lambda: os.getenv("BACKEND_FFPROBE_BINARY", "ffprobe")
    )
    media_command_timeout_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("BACKEND_MEDIA_COMMAND_TIMEOUT_SECONDS", "90")
        )
    )
    max_audio_bytes: int = 50 * 1024 * 1024
    max_model_bytes: int = 500 * 1024 * 1024
    max_video_bytes: int = 2 * 1024 * 1024 * 1024
    chunk_size: int = 1024 * 1024
    worker_poll_seconds: float = 1.0
    job_lease_seconds: float = 60.0

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir).resolve()
        self.storage_dir = Path(self.storage_dir or self.base_dir / "storage").resolve()
        self.database_path = Path(self.database_path or self.storage_dir / "packages.db").resolve()
        self.object_store_dir = Path(
            self.object_store_dir or self.storage_dir / "objects"
        ).resolve()
        self.object_staging_dir = Path(
            self.object_staging_dir or self.storage_dir / "object-staging"
        ).resolve()
        if self.worker_poll_seconds <= 0:
            raise ValueError("worker_poll_seconds must be positive")
        if self.job_lease_seconds <= 0:
            raise ValueError("job_lease_seconds must be positive")
        if self.media_command_timeout_seconds <= 0:
            raise ValueError("media_command_timeout_seconds must be positive")

    @property
    def packages_dir(self) -> Path:
        return self.storage_dir / "packages"

    @property
    def staging_dir(self) -> Path:
        return self.storage_dir / "staging"

    @property
    def allowed_origins(self) -> list[str]:
        values = [value.strip() for value in self.cors_origins.split(",") if value.strip()]
        return values or ["*"]

