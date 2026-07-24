from __future__ import annotations

from pydantic import BaseModel


class UserTokenIssued(BaseModel):
    user_id: str
    token: str
    token_type: str = "Bearer"


class ContentCreated(BaseModel):
    content_id: str
    state: str
    display_label: str
    status_url: str


class ContentSource(BaseModel):
    filename: str
    content_type: str
    byte_length: int
    sha256: str


class ContentSummary(BaseModel):
    content_id: str
    state: str
    processing_stage: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    display_label: str
    duration_ms: int | None = None
    created_at: str
    updated_at: str
    ready_at: str | None = None
    deleted_at: str | None = None
    status_url: str
    nfc_url: str | None = None
    source: ContentSource


class ContentList(BaseModel):
    items: list[ContentSummary]
    total: int
