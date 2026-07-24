from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from .audio_normalizer import AudioNormalizer
from .database import Database
from .frame_renderer import HeadlessFrameRenderer
from .job_repository import ClaimedJob
from .media_tools import MediaTools
from .mp3_index import Mp3Indexer
from .object_store import FileSystemObjectStore
from .package_publisher import ReadyPackagePublisher
from .processing_worker import StageReporter, TerminalProcessingError
from .video_encoder import H264VideoEncoder


class CloudMediaProcessor:
    """Run the complete deterministic cloud media pipeline for one claimed job."""

    def __init__(
        self,
        *,
        database: Database,
        object_store: FileSystemObjectStore,
        media_tools: MediaTools,
        frame_renderer: HeadlessFrameRenderer,
    ) -> None:
        self.database = database
        self.object_store = object_store
        self.audio_normalizer = AudioNormalizer(
            database=database,
            object_store=object_store,
            media_tools=media_tools,
        )
        self.mp3_indexer = Mp3Indexer(object_store)
        self.frame_renderer = frame_renderer
        self.video_encoder = H264VideoEncoder(
            object_store=object_store,
            media_tools=media_tools,
        )
        self.publisher = ReadyPackagePublisher(
            database=database,
            object_store=object_store,
        )

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
        with tempfile.TemporaryDirectory(prefix="cloud-render-") as directory:
            frames_dir = Path(directory) / "frames"
            await reporter.stage("RENDERING_VIDEO")
            await asyncio.to_thread(
                self.frame_renderer.render,
                job,
                audio_key=normalized.mp3_key,
                duration_ms=normalized.duration_ms,
                seed=int(content["visual_seed"]),
                output_dir=frames_dir,
            )
            await self.video_encoder.encode(
                job,
                reporter,
                frames_dir=frames_dir,
                authoritative_duration_ms=normalized.duration_ms,
            )
        await self.publisher.publish(job, reporter)
