from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from datetime import datetime, timezone

from .audio_normalizer import AudioNormalizer
from .database import Database
from .job_repository import ClaimedJob
from .media_tools import MediaTools
from .mp3_index import Mp3Indexer
from .object_store import FileSystemObjectStore
from .processing_worker import StageReporter, TerminalProcessingError


class CloudMediaProcessor:
    """Normalize audio and build index. Video is uploaded separately by the client."""

    def __init__(
        self,
        *,
        database: Database,
        object_store: FileSystemObjectStore,
        media_tools: MediaTools,
    ) -> None:
        self.database = database
        self.object_store = object_store
        self.audio_normalizer = AudioNormalizer(
            database=database,
            object_store=object_store,
            media_tools=media_tools,
        )
        self.mp3_indexer = Mp3Indexer(object_store)

    async def process(self, job: ClaimedJob, reporter: StageReporter) -> None:
        content = await asyncio.to_thread(self.database.get_content, job.content_id)
        if content is None or content["job_id"] != job.job_id:
            raise TerminalProcessingError(
                "CONTENT_JOB_MISMATCH",
                "内容处理任务不存在",
            )

        normalized = await self.audio_normalizer.normalize(job, reporter)
        await reporter.stage("BUILDING_AUDIO_INDEX")
        await asyncio.to_thread(
            self.mp3_indexer.build,
            job,
            authoritative_duration_ms=normalized.duration_ms,
        )

        await asyncio.to_thread(
            self._publish_audio,
            job,
            duration_ms=normalized.duration_ms,
            mp3_key=normalized.mp3_key,
        )

    def _publish_audio(
        self,
        job: ClaimedJob,
        *,
        duration_ms: int,
        mp3_key: str,
    ) -> None:
        with self.object_store.open_staging(mp3_key) as f:
            payload = f.read()
        sha256 = hashlib.sha256(payload).hexdigest()
        etag = f'"{sha256[:32]}"'
        object_key = f"contents/{job.content_id}/output/audio.mp3"
        self.object_store.put(object_key, io.BytesIO(payload))

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.database.connect() as conn:
            conn.execute(
                "INSERT INTO media_objects (id, content_id, kind, object_key, content_type, byte_length, sha256, etag, created_at) "
                "VALUES (?, ?, 'AUDIO', ?, 'audio/mpeg', ?, ?, ?, ?) "
                "ON CONFLICT(content_id, kind) DO UPDATE SET "
                "object_key=?, content_type='audio/mpeg', byte_length=?, sha256=?, etag=?, created_at=?",
                (uuid.uuid4().hex, job.content_id, object_key, len(payload), sha256, etag, now,
                 object_key, len(payload), sha256, etag, now),
            )
            conn.execute(
                "UPDATE contents SET duration_ms=?, updated_at=? WHERE id=?",
                (duration_ms, now, job.content_id),
            )
