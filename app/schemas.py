from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    detail: str = Field(examples=["无效或缺少用户 Token"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: Literal["ready"] = "ready"
    worker: Literal["running", "disabled"]


class UserTokenIssued(BaseModel):
    user_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    token: str = Field(description="Opaque User Token. Returned once and not stored in plaintext.")
    token_type: Literal["Bearer"] = "Bearer"


EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class EmailRegisterRequest(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=254, examples=["user@example.com"])
    password: str = Field(min_length=8, max_length=128)


class EmailRegistered(BaseModel):
    user_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    email: str = Field(pattern=EMAIL_PATTERN)


class EmailLoginRequest(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class WalletChallengeRequest(BaseModel):
    address: str = Field(
        pattern=r"^0x[0-9a-fA-F]{40}$",
        description="Injective EVM wallet address (0x + 40 hex).",
        examples=["0x1234567890abcdef1234567890abcdef12345678"],
    )


class WalletChallengeResponse(BaseModel):
    address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    message: str = Field(description="Exact text the wallet must sign via personal_sign.")
    expires_at: str


class WalletVerifyRequest(BaseModel):
    address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    signature: str = Field(
        pattern=r"^0x[0-9a-fA-F]+$",
        description="EIP-191 personal_sign signature over the challenge message.",
    )


class WalletTokenIssued(BaseModel):
    user_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    wallet_address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    token: str = Field(description="Opaque User Token. Returned once and not stored in plaintext.")
    token_type: Literal["Bearer"] = "Bearer"


class UserProfile(BaseModel):
    user_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    wallet_address: str | None = None
    email: str | None = None


class ContentCreated(BaseModel):
    content_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    state: Literal["UPLOADED"]
    display_label: str
    status_url: str


class ContentSource(BaseModel):
    filename: str
    content_type: str
    byte_length: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ContentSummary(BaseModel):
    content_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    state: Literal["UPLOADED", "PROCESSING", "READY", "FAILED", "DELETED"]
    processing_stage: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    display_label: str
    duration_ms: int | None = Field(default=None, ge=1, le=30_000)
    created_at: str
    updated_at: str
    ready_at: str | None = None
    deleted_at: str | None = None
    status_url: str
    nfc_url: str | None = None
    source: ContentSource


class ContentList(BaseModel):
    items: list[ContentSummary]
    total: int = Field(ge=0)


class ContentPage(BaseModel):
    items: list[ContentSummary]
    total: int = Field(ge=0)
    page_number: int = Field(ge=1)
    units_per_page: int = Field(ge=1)
    total_pages: int = Field(ge=0)


class TriggerPresentation(BaseModel):
    display_label: str
    autoplay: bool
    end_behavior: Literal["HOLD_LAST_FRAME"]
    controls: list[Literal["PLAY", "PAUSE", "SEEK", "STOP", "REPLAY"]]


class PlaybackVideoDescriptor(BaseModel):
    url: str
    format: Literal["mp4"]
    codec: Literal["h264"]
    profile: Literal["Constrained Baseline"]
    pixel_format: Literal["yuv420p"]
    width: Literal[480]
    height: Literal[320]
    fps: Literal[10]
    duration_ms: int = Field(ge=1, le=30_100)
    max_keyframe_interval_ms: int = Field(le=1_000)
    byte_length: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    etag: str


class PlaybackAudioDescriptor(BaseModel):
    url: str
    index_url: str
    format: Literal["mp3"]
    bitrate: Literal[128_000]
    sample_rate: Literal[44_100]
    channels: Literal[1, 2]
    byte_length: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    etag: str
    index_version: Literal[1]
    index_byte_length: int = Field(ge=16)
    index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_etag: str


class PlaybackDescriptor(BaseModel):
    profile: Literal["t5ai-h264-mp3-v1"]
    video: PlaybackVideoDescriptor
    audio: PlaybackAudioDescriptor


class CompactContent(BaseModel):
    schema_version: Literal[1]
    content_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    state: Literal["READY"]
    duration_ms: int = Field(ge=1, le=30_000)
    trigger: TriggerPresentation
    playback: PlaybackDescriptor
