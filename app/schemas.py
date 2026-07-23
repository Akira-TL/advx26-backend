from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


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

