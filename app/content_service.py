from __future__ import annotations

import hashlib
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from fastapi import UploadFile

from .database import Database
from .object_store import FileSystemObjectStore


class EmptySourceAudio(ValueError):
    pass


class SourceAudioTooLarge(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UploadedContent:
    content_id: str
    state: str
    display_label: str


class ContentService:
    def __init__(
        self,
        *,
        database: Database,
        object_store: FileSystemObjectStore,
        max_audio_bytes: int,
        chunk_size: int,
        max_attempts: int = 3,
    ) -> None:
        if max_audio_bytes <= 0:
            raise ValueError("max_audio_bytes must be positive")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.database = database
        self.object_store = object_store
        self.max_audio_bytes = max_audio_bytes
        self.chunk_size = chunk_size
        self.max_attempts = max_attempts

    async def upload(self, *, owner_user_id: str, audio: UploadFile) -> UploadedContent:
        content_id = uuid.uuid4().hex
        source_object_key = f"contents/{content_id}/source/original"
        raw_filename = (audio.filename or "audio").replace("\\", "/")
        source_filename = Path(raw_filename).name or "audio"
        source_content_type = audio.content_type or "application/octet-stream"
        now = _utc_now()
        display_label = Path(source_filename).stem or f"声音碎片 #{content_id[:4].upper()}"
        visual_seed = _visual_seed(content_id)

        spool, byte_length, sha256 = await self._read_source(audio)
        try:
            self.object_store.put(source_object_key, spool)
            try:
                self.database.create_uploaded_content(
                    content_id=content_id,
                    owner_user_id=owner_user_id,
                    display_label=display_label,
                    visual_seed=visual_seed,
                    source_object_key=source_object_key,
                    source_filename=source_filename,
                    source_content_type=source_content_type,
                    source_byte_length=byte_length,
                    source_sha256=sha256,
                    job_id=uuid.uuid4().hex,
                    media_object_id=uuid.uuid4().hex,
                    max_attempts=self.max_attempts,
                    created_at=now,
                )
            except Exception:
                self.object_store.delete(source_object_key)
                raise
        finally:
            spool.close()

        return UploadedContent(
            content_id=content_id,
            state="UPLOADED",
            display_label=display_label,
        )

    async def _read_source(self, upload: UploadFile) -> tuple[BinaryIO, int, str]:
        digest = hashlib.sha256()
        size = 0
        spool = tempfile.SpooledTemporaryFile(
            max_size=min(self.max_audio_bytes, 8 * 1024 * 1024),
            mode="w+b",
        )
        try:
            while chunk := await upload.read(self.chunk_size):
                size += len(chunk)
                if size > self.max_audio_bytes:
                    raise SourceAudioTooLarge(
                        f"音频超过限制：{self.max_audio_bytes} bytes"
                    )
                digest.update(chunk)
                spool.write(chunk)
            if size == 0:
                raise EmptySourceAudio("不允许上传空音频")
            spool.seek(0)
            return spool, size, digest.hexdigest()
        except Exception:
            spool.close()
            raise
        finally:
            await upload.close()


def _visual_seed(content_id: str) -> int:
    raw = hashlib.sha256(content_id.encode("ascii")).digest()[:8]
    return int.from_bytes(raw, "big") & ((1 << 63) - 1)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
