from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root so os.getenv() picks up values below.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    base_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1])
    storage_dir: Path | None = None
    database_path: Path | None = None
    object_store_dir: Path | None = None
    object_staging_dir: Path | None = None
    renderer_project_dir: Path | None = None
    public_base_url: str = field(
        default_factory=lambda: os.getenv(
            "BACKEND_PUBLIC_BASE_URL",
            "http://127.0.0.1:9000",
        )
    )
    trigger_token: str = field(
        default_factory=lambda: os.getenv("BACKEND_TRIGGER_TOKEN", "")
    )
    playback_token: str = field(
        default_factory=lambda: os.getenv("BACKEND_PLAYBACK_TOKEN", "")
    )
    wallet_auth_domain: str = field(
        default_factory=lambda: os.getenv(
            "BACKEND_WALLET_AUTH_DOMAIN",
            "AdventureX Cloud Media",
        )
    )
    wallet_nonce_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("BACKEND_WALLET_NONCE_TTL_SECONDS", "300"))
    )
    cors_origins: str = field(default_factory=lambda: os.getenv("BACKEND_CORS_ORIGINS", "*"))
    ffmpeg_binary: str = field(
        default_factory=lambda: os.getenv("BACKEND_FFMPEG_BINARY", "ffmpeg")
    )
    ffprobe_binary: str = field(
        default_factory=lambda: os.getenv("BACKEND_FFPROBE_BINARY", "ffprobe")
    )
    node_binary: str = field(
        default_factory=lambda: os.getenv("BACKEND_NODE_BINARY", "node")
    )
    media_command_timeout_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("BACKEND_MEDIA_COMMAND_TIMEOUT_SECONDS", "90")
        )
    )
    renderer_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("BACKEND_RENDERER_TIMEOUT_SECONDS", "180"))
    )
    max_audio_bytes: int = field(
        default_factory=lambda: int(os.getenv("BACKEND_MAX_AUDIO_BYTES", str(50 * 1024 * 1024)))
    )
    chunk_size: int = 1024 * 1024
    worker_enabled: bool = field(
        default_factory=lambda: _env_bool("BACKEND_WORKER_ENABLED", False)
    )
    worker_poll_seconds: float = field(
        default_factory=lambda: float(os.getenv("BACKEND_WORKER_POLL_SECONDS", "1"))
    )
    job_lease_seconds: float = field(
        default_factory=lambda: float(os.getenv("BACKEND_JOB_LEASE_SECONDS", "60"))
    )
    processing_max_attempts: int = field(
        default_factory=lambda: int(os.getenv("BACKEND_PROCESSING_MAX_ATTEMPTS", "3"))
    )
    failed_staging_retention_seconds: int = field(
        default_factory=lambda: int(
            os.getenv("BACKEND_FAILED_STAGING_RETENTION_SECONDS", str(24 * 60 * 60))
        )
    )
    failed_staging_max_bytes: int = field(
        default_factory=lambda: int(
            os.getenv("BACKEND_FAILED_STAGING_MAX_BYTES", str(512 * 1024 * 1024))
        )
    )
    chain_rpc_url: str = field(
        default_factory=lambda: os.getenv(
            "BACKEND_CHAIN_RPC_URL",
            "https://k8s.testnet.json-rpc.injective.network/",
        )
    )
    chain_id: int = field(
        default_factory=lambda: int(os.getenv("BACKEND_CHAIN_ID", "1439"))
    )
    chain_contract_address: str = field(
        default_factory=lambda: os.getenv("BACKEND_CHAIN_CONTRACT_ADDRESS", "")
    )
    chain_operator_private_key: str = field(
        default_factory=lambda: os.getenv("BACKEND_CHAIN_OPERATOR_PRIVATE_KEY", "")
    )
    chain_enabled: bool = field(
        default_factory=lambda: _env_bool("BACKEND_CHAIN_ENABLED", False)
    )

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir).resolve()
        self.storage_dir = Path(self.storage_dir or self.base_dir / "storage").resolve()
        self.database_path = Path(
            self.database_path or self.storage_dir / "cloud-media.db"
        ).resolve()
        self.object_store_dir = Path(
            self.object_store_dir or self.storage_dir / "objects"
        ).resolve()
        self.object_staging_dir = Path(
            self.object_staging_dir or self.storage_dir / "object-staging"
        ).resolve()
        self.renderer_project_dir = Path(
            self.renderer_project_dir
            or self.base_dir
            / "Sound-Visualization-Kaleidoscope-effect"
            / "particle-field"
        ).resolve()
        self.public_base_url = self.public_base_url.strip().rstrip("/")
        if not self.public_base_url.startswith(("http://", "https://")):
            raise ValueError("public_base_url must start with http:// or https://")
        positive_values = {
            "media_command_timeout_seconds": self.media_command_timeout_seconds,
            "renderer_timeout_seconds": self.renderer_timeout_seconds,
            "max_audio_bytes": self.max_audio_bytes,
            "chunk_size": self.chunk_size,
            "worker_poll_seconds": self.worker_poll_seconds,
            "job_lease_seconds": self.job_lease_seconds,
            "processing_max_attempts": self.processing_max_attempts,
        }
        for name, value in positive_values.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.failed_staging_retention_seconds < 0:
            raise ValueError("failed_staging_retention_seconds must be non-negative")
        if self.failed_staging_max_bytes < 0:
            raise ValueError("failed_staging_max_bytes must be non-negative")

    @property
    def allowed_origins(self) -> list[str]:
        values = [value.strip() for value in self.cors_origins.split(",") if value.strip()]
        return values or ["*"]
