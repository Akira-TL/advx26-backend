from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.database import Database
from app.job_repository import ClaimedJob, ProcessingJobRepository
from app.object_store import FileSystemObjectStore
from app.package_publisher import ReadyPackagePublisher
from app.processing_worker import TerminalProcessingError, TransientProcessingError


class Reporter:
    def __init__(self) -> None:
        self.stages: list[str] = []

    async def stage(self, name: str) -> None:
        self.stages.append(name)


class FailOnceStore(FileSystemObjectStore):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fail_next_promotion = True

    def promote_staging(self, staging_prefix: str, destination_prefix: str) -> None:
        if self.fail_next_promotion:
            self.fail_next_promotion = False
            raise OSError("injected promotion failure")
        super().promote_staging(staging_prefix, destination_prefix)


class FailOnceDatabase(Database):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.fail_next_publication = True

    def publish_ready_content(self, **kwargs) -> None:
        if self.fail_next_publication:
            self.fail_next_publication = False
            raise sqlite3.OperationalError("injected database failure")
        super().publish_ready_content(**kwargs)


class PackagePublisherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def create_context(
        self,
        *,
        database_type=Database,
        store_type=FileSystemObjectStore,
    ):
        database = database_type(self.root / f"{uuid.uuid4().hex}.db")
        database.initialize()
        store = store_type(
            objects_root=self.root / f"objects-{uuid.uuid4().hex}",
            staging_root=self.root / f"staging-{uuid.uuid4().hex}",
        )
        store.initialize()
        repository = ProcessingJobRepository(database)
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        user_id = uuid.uuid4().hex
        content_id = uuid.uuid4().hex
        job_id = uuid.uuid4().hex
        source_key = f"contents/{content_id}/source/original"
        source_payload = b"original-source-audio"
        store.put(source_key, io.BytesIO(source_payload))
        database.create_user_token(
            user_id=user_id,
            token_id=uuid.uuid4().hex,
            token_digest=uuid.uuid4().hex,
            token_hint="test",
            created_at=created_at,
        )
        database.create_uploaded_content(
            content_id=content_id,
            owner_user_id=user_id,
            display_label="声音碎片 #TEST",
            visual_seed=123,
            source_object_key=source_key,
            source_filename="voice.wav",
            source_content_type="audio/wav",
            source_byte_length=len(source_payload),
            source_sha256=hashlib.sha256(source_payload).hexdigest(),
            job_id=job_id,
            media_object_id=uuid.uuid4().hex,
            created_at=created_at,
        )
        database.update_content_duration(content_id, 1150)
        job = repository.claim_next(
            worker_id="publisher-worker",
            now=created_at,
            lease_seconds=60,
        )
        assert job is not None
        self.stage_generated_objects(store, job)
        return database, store, job, user_id, source_key, source_payload

    @staticmethod
    def stage_generated_objects(store: FileSystemObjectStore, job: ClaimedJob) -> None:
        audio = b"normalized-mp3-payload"
        index = b"AIX1-index-payload"
        video = b"fast-start-h264-mp4-payload"
        audio_key = f"jobs/{job.job_id}/normalized/audio.mp3"
        index_key = f"jobs/{job.job_id}/indexed/audio.idx"
        video_key = f"jobs/{job.job_id}/video/video.mp4"
        store.replace_staging(audio_key, io.BytesIO(audio))
        store.replace_staging(index_key, io.BytesIO(index))
        store.replace_staging(video_key, io.BytesIO(video))

        def descriptor(payload: bytes, key: str) -> dict[str, object]:
            digest = hashlib.sha256(payload).hexdigest()
            return {
                "object_key": key,
                "byte_length": len(payload),
                "sha256": digest,
                "etag": f'"{digest}"',
            }

        audio_metadata = {
            "schema_version": 1,
            "content_id": job.content_id,
            "duration_ms": 1150,
            "audio": {
                **descriptor(audio, audio_key),
                "sample_rate": 44_100,
                "bit_rate": 128_000,
                "channels": 1,
            },
            "index": {
                **descriptor(index, index_key),
                "version": 1,
                "record_count": 10,
            },
        }
        video_metadata = {
            "schema_version": 1,
            "content_id": job.content_id,
            **descriptor(video, video_key),
            "duration_ms": 1200,
            "frame_count": 12,
            "width": 480,
            "height": 320,
            "frame_rate": 10,
            "codec": "h264",
            "profile": "Constrained Baseline",
            "pixel_format": "yuv420p",
            "max_keyframe_interval_frames": 10,
            "has_b_frames": False,
            "fast_start": True,
        }
        store.replace_staging(
            f"jobs/{job.job_id}/indexed/audio-index.json",
            io.BytesIO(json.dumps(audio_metadata).encode()),
        )
        store.replace_staging(
            f"jobs/{job.job_id}/video/video.json",
            io.BytesIO(json.dumps(video_metadata).encode()),
        )
        replay_params = {
            "seed": 7,
            "durationMs": 1150,
            "width": 480,
            "height": 320,
            "fps": 10,
            "quality": "medium",
        }
        store.replace_staging(
            f"jobs/{job.job_id}/replay/replay-params.json",
            io.BytesIO(json.dumps(replay_params).encode()),
        )

    async def test_publishes_complete_ready_package_and_preserves_owner_source(self) -> None:
        database, store, job, user_id, source_key, source_payload = self.create_context()
        reporter = Reporter()
        publisher = ReadyPackagePublisher(database=database, object_store=store)

        published = await publisher.publish(job, reporter)

        self.assertEqual(reporter.stages, ["VALIDATING"])
        content = database.get_content(job.content_id)
        self.assertEqual(content["state"], "READY")
        self.assertEqual(content["owner_user_id"], user_id)
        self.assertEqual(content["source_object_key"], source_key)
        with store.open(source_key) as source:
            self.assertEqual(source.read(), source_payload)
        with store.open(published.manifest_key) as source:
            manifest = json.load(source)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["content_id"], job.content_id)
        self.assertEqual(manifest["state"], "READY")
        self.assertEqual(manifest["playback"]["profile"], "t5ai-h264-mp3-v1")
        self.assertEqual(manifest["trigger"]["display_label"], "声音碎片 #TEST")
        self.assertEqual(manifest["playback"]["video"]["url"], f"/api/v1/contents/{job.content_id}/assets/video")
        self.assertEqual(manifest["playback"]["audio"]["index_version"], 1)
        media = database.get_media_objects(job.content_id)
        self.assertEqual({row["kind"] for row in media}, {"SOURCE", "VIDEO", "AUDIO", "AUDIO_INDEX", "MANIFEST", "REPLAY_PARAMS"})

    async def test_promotion_failure_leaves_content_non_ready_and_retry_succeeds(self) -> None:
        database, store, job, *_ = self.create_context(store_type=FailOnceStore)
        publisher = ReadyPackagePublisher(database=database, object_store=store)

        with self.assertRaises(TransientProcessingError) as captured:
            await publisher.publish(job, Reporter())

        self.assertEqual(captured.exception.code, "PUBLICATION_STORAGE_TEMPORARY")
        self.assertEqual(database.get_content(job.content_id)["state"], "PROCESSING")
        with self.assertRaises(FileNotFoundError):
            store.stat(f"contents/{job.content_id}/output/manifest.json")

        await publisher.publish(job, Reporter())
        self.assertEqual(database.get_content(job.content_id)["state"], "READY")

    async def test_database_failure_after_promotion_is_invisible_until_retry(self) -> None:
        database, store, job, *_ = self.create_context(database_type=FailOnceDatabase)
        publisher = ReadyPackagePublisher(database=database, object_store=store)

        with self.assertRaises(TransientProcessingError) as captured:
            await publisher.publish(job, Reporter())

        self.assertEqual(captured.exception.code, "PUBLICATION_DATABASE_TEMPORARY")
        self.assertEqual(database.get_content(job.content_id)["state"], "PROCESSING")
        self.assertTrue(store.stat(f"contents/{job.content_id}/output/manifest.json"))

        await publisher.publish(job, Reporter())
        self.assertEqual(database.get_content(job.content_id)["state"], "READY")

    async def test_integrity_failure_never_promotes_partial_package(self) -> None:
        database, store, job, *_ = self.create_context()
        key = f"jobs/{job.job_id}/video/video.mp4"
        store.replace_staging(key, io.BytesIO(b"tampered"))
        publisher = ReadyPackagePublisher(database=database, object_store=store)

        with self.assertRaises(TerminalProcessingError) as captured:
            await publisher.publish(job, Reporter())

        self.assertEqual(captured.exception.code, "PUBLICATION_INVALID")
        self.assertEqual(database.get_content(job.content_id)["state"], "PROCESSING")
        with self.assertRaises(FileNotFoundError):
            store.stat(f"contents/{job.content_id}/output/video.mp4")


if __name__ == "__main__":
    unittest.main()
