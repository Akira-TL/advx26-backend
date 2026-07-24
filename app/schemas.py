from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


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


class Creator(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)


class PackageMetadata(BaseModel):
    title: str = Field(default="", max_length=256)
    description: str = Field(default="", max_length=4000)
    creator: Creator
    created_at: datetime | None = None
    tags: list[str] = Field(default_factory=list, max_length=50)
    custom_metadata: dict[str, Any] = Field(default_factory=dict)


class PackageCreated(BaseModel):
    package_id: str
    status: str
    manifest_url: str


class PackageSummary(BaseModel):
    package_id: str
    title: str
    description: str
    creator: Creator
    created_at: str
    received_at: str
    status: str
    tags: list[str]
    total_size: int


class PackageList(BaseModel):
    items: list[PackageSummary]
    total: int
    limit: int
    offset: int

