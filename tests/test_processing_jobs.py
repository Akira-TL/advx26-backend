from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.config import Settings
from app.database import Database
from app.job_repository import ProcessingJobRepository
from app.main import create_app
from app.processing_worker import (
    ProcessingWorker,
    TerminalProcessingError,
    TransientProcessingError,
)

from tests.helpers import register_and_login


def utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def seed_job(database: Database, *, suffix: str = "1") -> str:
    now = "2026-07-24T12:00:00Z"
    user_id = f"user-{suffix}"
    content_id = uuid.uuid4().hex
    database.create_user_token(
        user_id=user_id,
        token_id=f"token-{suffix}",
        token_digest=f"digest-{suffix}",
        token_hint=suffix,
        created_at=now,
    )
    database.create_uploaded_content(
        content_id=content_id,
        owner_user_id=user_id,
        display_label=f"声音碎片 #{suffix}",
        visual_seed=1,
        source_object_key=f"contents/{content_id}/source/original",
        source_filename="voice.wav",
        source_content_type="audio/wav",
        source_byte_length=4,
        source_sha256="a" * 64,
        job_id=uuid.uuid4().hex,
        media_object_id=uuid.uuid4().hex,
        max_attempts=3,
        created_at=now,
    )
    return content_id


class ProcessingJobRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "jobs.db")
        self.database.initialize()
        self.repository = ProcessingJobRepository(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_claim_is_exclusive_and_expired_claim_is_recoverable(self) -> None:
        content_id = seed_job(self.database)
        start = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

        first = self.repository.claim_next(
            worker_id="worker-a",
            now=utc(start),
            lease_seconds=30,
        )
        duplicate = self.repository.claim_next(
            worker_id="worker-b",
            now=utc(start + timedelta(seconds=10)),
            lease_seconds=30,
        )
        recovered = self.repository.claim_next(
            worker_id="worker-b",
            now=utc(start + timedelta(seconds=31)),
            lease_seconds=30,
        )

        self.assertIsNotNone(first)
        self.assertEqual(first.content_id, content_id)
        self.assertEqual(first.attempt, 1)
        self.assertIsNone(duplicate)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.content_id, content_id)
        self.assertEqual(recovered.worker_id, "worker-b")
        self.assertEqual(recovered.attempt, 2)


class ScriptedProcessor:
    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def process(self, job, reporter) -> None:
        self.calls += 1
        await reporter.stage("PROBING")
        outcome = self.outcomes.pop(0)
        if outcome == "transient":
            raise TransientProcessingError("RENDER_TEMPORARY", "可视化渲染暂时失败")
        if outcome == "terminal":
            raise TerminalProcessingError("SOURCE_INVALID", "音频无法解析")
        await reporter.stage("NORMALIZING_AUDIO")
        await reporter.stage("RENDERING_VIDEO")
        await reporter.stage("ENCODING_VIDEO")
        await reporter.stage("BUILDING_AUDIO_INDEX")
        await reporter.stage("VALIDATING")


class ProcessingWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "worker.db")
        self.database.initialize()
        self.repository = ProcessingJobRepository(self.database)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_transient_failure_retries_then_completes_same_content(self) -> None:
        content_id = seed_job(self.database)
        processor = ScriptedProcessor(["transient", "success"])
        worker = ProcessingWorker(
            repository=self.repository,
            processor=processor,
            worker_id="worker-a",
            lease_seconds=60,
            poll_seconds=0.01,
        )

        self.assertTrue(await worker.run_once())
        after_failure = self.repository.get_for_content(content_id)
        self.assertEqual(after_failure["status"], "RETRY")
        self.assertEqual(after_failure["attempt"], 1)
        self.assertEqual(after_failure["content_state"], "PROCESSING")
        self.assertEqual(after_failure["error_code"], "RENDER_TEMPORARY")

        self.assertTrue(await worker.run_once())
        completed = self.repository.get_for_content(content_id)
        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(completed["stage"], "READY")
        self.assertEqual(completed["content_state"], "READY")
        self.assertEqual(completed["attempt"], 2)
        self.assertEqual(processor.calls, 2)
        self.assertFalse(await worker.run_once())

    async def test_transient_failure_stops_after_retry_budget(self) -> None:
        content_id = seed_job(self.database)
        processor = ScriptedProcessor(["transient", "transient", "transient"])
        worker = ProcessingWorker(
            repository=self.repository,
            processor=processor,
            worker_id="worker-a",
            lease_seconds=60,
            poll_seconds=0.01,
        )

        self.assertTrue(await worker.run_once())
        self.assertTrue(await worker.run_once())
        self.assertTrue(await worker.run_once())
        exhausted = self.repository.get_for_content(content_id)

        self.assertEqual(exhausted["status"], "FAILED")
        self.assertEqual(exhausted["content_state"], "FAILED")
        self.assertEqual(exhausted["attempt"], 3)
        self.assertEqual(exhausted["error_code"], "RENDER_TEMPORARY")
        self.assertFalse(await worker.run_once())

    async def test_terminal_failure_does_not_retry_and_exposes_safe_error(self) -> None:
        content_id = seed_job(self.database)
        worker = ProcessingWorker(
            repository=self.repository,
            processor=ScriptedProcessor(["terminal"]),
            worker_id="worker-a",
            lease_seconds=60,
            poll_seconds=0.01,
        )

        self.assertTrue(await worker.run_once())
        failed = self.repository.get_for_content(content_id)

        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["content_state"], "FAILED")
        self.assertEqual(failed["error_code"], "SOURCE_INVALID")
        self.assertEqual(failed["error_message"], "音频无法解析")
        self.assertEqual(failed["attempt"], 1)


class BlockingProcessor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def process(self, job, reporter) -> None:
        await reporter.stage("PROBING")
        await reporter.stage("NORMALIZING_AUDIO")
        await reporter.stage("RENDERING_VIDEO")
        self.started.set()
        await self.release.wait()
        await reporter.stage("ENCODING_VIDEO")
        await reporter.stage("BUILDING_AUDIO_INDEX")
        await reporter.stage("VALIDATING")


class ProcessingWorkerApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.processor = BlockingProcessor()
        settings = Settings(
            base_dir=root,
            storage_dir=root / "storage",
            database_path=root / "storage" / "jobs.db",
            object_store_dir=root / "storage" / "objects",
            object_staging_dir=root / "storage" / "object-staging",
            worker_poll_seconds=0.01,
            job_lease_seconds=30,
        )
        self.app = create_app(settings, job_processor=self.processor)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self) -> None:
        self.processor.release.set()
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.temporary.cleanup()

    async def test_lifespan_worker_reports_stage_without_blocking_health(self) -> None:
        issued = await register_and_login(self.client)
        auth = {"Authorization": f"Bearer {issued['token']}"}
        created = await self.client.post(
            "/api/v1/contents",
            headers=auth,
            files={"audio": ("voice.wav", b"worker-audio", "audio/wav")},
        )
        self.assertEqual(created.status_code, 201, created.text)
        content_id = created.json()["content_id"]

        await asyncio.wait_for(self.processor.started.wait(), timeout=2)
        health = await self.client.get("/api/v1/health")
        status = await self.client.get(f"/api/v1/contents/{content_id}", headers=auth)

        self.assertEqual(health.status_code, 200)
        self.assertEqual(status.json()["state"], "PROCESSING")
        self.assertEqual(status.json()["processing_stage"], "RENDERING_VIDEO")
        self.assertIsNone(status.json()["error_code"])

        self.processor.release.set()
        for _ in range(100):
            status = await self.client.get(
                f"/api/v1/contents/{content_id}", headers=auth
            )
            if status.json()["state"] == "READY":
                break
            await asyncio.sleep(0.01)

        self.assertEqual(status.json()["state"], "READY")
        self.assertEqual(status.json()["processing_stage"], "READY")


if __name__ == "__main__":
    unittest.main()
