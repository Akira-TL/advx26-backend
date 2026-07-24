from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.config import Settings
from app.job_repository import ProcessingJobRepository
from app.lifecycle import StagingCleanup
from app.main import create_app

from tests.helpers import register_and_login


def utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ContentLifecycleApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(
            base_dir=root,
            storage_dir=root / "storage",
            database_path=root / "storage" / "lifecycle.db",
            object_store_dir=root / "storage" / "objects",
            object_staging_dir=root / "storage" / "object-staging",
            trigger_token="trigger-lifecycle",
            playback_token="playback-lifecycle",
        )
        self.app = create_app(self.settings)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        )
        first = await register_and_login(self.client)
        second = await register_and_login(self.client)
        self.owner_auth = {"Authorization": f"Bearer {first['token']}"}
        self.other_auth = {"Authorization": f"Bearer {second['token']}"}
        self.owner_id = first["user_id"]
        self.trigger_auth = {"Authorization": "Bearer trigger-lifecycle"}

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.temporary.cleanup()

    async def upload(self, payload: bytes = b"source-audio") -> str:
        response = await self.client.post(
            "/api/v1/contents",
            headers=self.owner_auth,
            files={"audio": ("voice.wav", payload, "audio/wav")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["content_id"]

    def fail_content(self, content_id: str, *, retryable: bool) -> None:
        database = self.app.state.database
        with database.connect() as connection:
            connection.execute(
                "UPDATE processing_jobs SET max_attempts = 1 WHERE content_id = ?",
                (content_id,),
            )
        repository = ProcessingJobRepository(database)
        claimed = repository.claim_next(
            worker_id=f"worker-{content_id[:4]}",
            now=utc(datetime.now(timezone.utc)),
            lease_seconds=60,
        )
        assert claimed is not None
        repository.fail(
            claimed,
            code="TEMPORARY_RENDER" if retryable else "SOURCE_INVALID",
            message="暂时失败" if retryable else "源文件无效",
            retryable=retryable,
            now=utc(datetime.now(timezone.utc)),
        )

    async def test_owner_status_retry_and_cross_user_isolation(self) -> None:
        content_id = await self.upload()
        self.fail_content(content_id, retryable=True)

        failed = await self.client.get(
            f"/api/v1/contents/{content_id}",
            headers=self.owner_auth,
        )
        self.assertEqual(failed.status_code, 200)
        self.assertEqual(failed.json()["state"], "FAILED")
        self.assertEqual(failed.json()["error_code"], "TEMPORARY_RENDER")
        self.assertIsNone(failed.json()["nfc_url"])

        for method in ("get", "post", "delete"):
            if method == "get":
                response = await self.client.get(
                    f"/api/v1/contents/{content_id}", headers=self.other_auth
                )
            elif method == "post":
                response = await self.client.post(
                    f"/api/v1/contents/{content_id}/retry", headers=self.other_auth
                )
            else:
                response = await self.client.delete(
                    f"/api/v1/contents/{content_id}", headers=self.other_auth
                )
            self.assertEqual(response.status_code, 404)

        retried = await self.client.post(
            f"/api/v1/contents/{content_id}/retry",
            headers=self.owner_auth,
        )
        self.assertEqual(retried.status_code, 202, retried.text)
        self.assertEqual(retried.json()["content_id"], content_id)
        self.assertEqual(retried.json()["state"], "PROCESSING")
        self.assertEqual(retried.json()["processing_stage"], "UPLOADED")
        self.assertIsNone(retried.json()["error_code"])
        job = ProcessingJobRepository(self.app.state.database).get_for_content(content_id)
        self.assertEqual(job["status"], "RETRY")
        self.assertEqual(job["attempt"], 0)

    async def test_terminal_source_failure_cannot_retry(self) -> None:
        content_id = await self.upload(b"invalid")
        self.fail_content(content_id, retryable=False)

        response = await self.client.post(
            f"/api/v1/contents/{content_id}/retry",
            headers=self.owner_auth,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            self.app.state.database.get_content(content_id)["state"],
            "FAILED",
        )

    async def test_owner_delete_immediately_revokes_device_access_but_keeps_objects(self) -> None:
        content_id = await self.upload()
        database = self.app.state.database
        store = self.app.state.object_store
        database.update_content_duration(content_id, 1000)
        manifest = {
            "schema_version": 1,
            "content_id": content_id,
            "state": "READY",
            "duration_ms": 1000,
            "trigger": {
                "display_label": "声音碎片 #DELETE",
                "autoplay": True,
                "end_behavior": "HOLD_LAST_FRAME",
                "controls": ["PLAY", "PAUSE", "SEEK", "STOP", "REPLAY"],
            },
            "playback": {
                "profile": "t5ai-h264-mp3-v1",
                "video": {"url": f"/api/v1/contents/{content_id}/assets/video"},
                "audio": {
                    "url": f"/api/v1/contents/{content_id}/assets/audio",
                    "index_url": f"/api/v1/contents/{content_id}/assets/audio-index",
                },
            },
        }
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(payload).hexdigest()
        manifest_key = f"contents/{content_id}/output/manifest.json"
        store.put(manifest_key, io.BytesIO(payload))
        database.publish_ready_content(
            content_id=content_id,
            duration_ms=1000,
            media_objects=[
                {
                    "id": uuid.uuid4().hex,
                    "kind": "MANIFEST",
                    "object_key": manifest_key,
                    "content_type": "application/json",
                    "byte_length": len(payload),
                    "sha256": digest,
                    "etag": f'"{digest}"',
                }
            ],
            ready_at=utc(datetime.now(timezone.utc)),
        )
        before = await self.client.get(f"/c/{content_id}", headers=self.trigger_auth)
        self.assertEqual(before.status_code, 200)

        deleted = await self.client.delete(
            f"/api/v1/contents/{content_id}",
            headers=self.owner_auth,
        )
        after = await self.client.get(f"/c/{content_id}", headers=self.trigger_auth)
        owner_view = await self.client.get(
            f"/api/v1/contents/{content_id}",
            headers=self.owner_auth,
        )

        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(after.status_code, 404)
        self.assertEqual(owner_view.status_code, 200)
        self.assertEqual(owner_view.json()["state"], "DELETED")
        self.assertIsNone(owner_view.json()["nfc_url"])
        with store.open(manifest_key) as source:
            self.assertEqual(source.read(), payload)


class StagingCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = __import__("app.database", fromlist=["Database"]).Database(
            root / "cleanup.db"
        )
        self.database.initialize()
        self.store = __import__(
            "app.object_store", fromlist=["FileSystemObjectStore"]
        ).FileSystemObjectStore(
            objects_root=root / "objects",
            staging_root=root / "staging",
        )
        self.store.initialize()
        self.user_id = "cleanup-user"
        base = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        self.database.create_user_token(
            user_id=self.user_id,
            token_id="cleanup-token",
            token_digest="cleanup-digest",
            token_hint="cleanup",
            created_at=utc(base),
        )
        self.base = base

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def seed_job(self, suffix: str, *, status: str, content_state: str, updated: datetime) -> str:
        content_id = uuid.uuid4().hex
        job_id = f"job-{suffix}"
        source_key = f"contents/{content_id}/source/original"
        source = f"source-{suffix}".encode()
        self.store.put(source_key, io.BytesIO(source))
        self.database.create_uploaded_content(
            content_id=content_id,
            owner_user_id=self.user_id,
            display_label=f"声音碎片 #{suffix}",
            visual_seed=1,
            source_object_key=source_key,
            source_filename="voice.wav",
            source_content_type="audio/wav",
            source_byte_length=len(source),
            source_sha256=hashlib.sha256(source).hexdigest(),
            job_id=job_id,
            media_object_id=uuid.uuid4().hex,
            created_at=utc(updated),
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE processing_jobs SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc(updated), job_id),
            )
            connection.execute(
                "UPDATE contents SET state = ?, updated_at = ? WHERE id = ?",
                (content_state, utc(updated), content_id),
            )
        self.store.replace_staging(
            f"jobs/{job_id}/diagnostic.bin",
            io.BytesIO(b"x" * 32),
        )
        return job_id

    def test_cleanup_removes_ready_and_expired_failed_but_skips_active_and_sources(self) -> None:
        ready = self.seed_job(
            "ready", status="COMPLETED", content_state="READY", updated=self.base
        )
        expired = self.seed_job(
            "expired",
            status="FAILED",
            content_state="FAILED",
            updated=self.base - timedelta(days=2),
        )
        active = self.seed_job(
            "active", status="CLAIMED", content_state="PROCESSING", updated=self.base
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE processing_jobs SET lease_expires_at = ? WHERE id = ?",
                (utc(self.base + timedelta(minutes=5)), active),
            )
        self.store.replace_staging("unrelated/keep.bin", io.BytesIO(b"keep"))

        report = StagingCleanup(
            database=self.database,
            object_store=self.store,
            failed_retention_seconds=24 * 60 * 60,
            failed_max_bytes=1024,
        ).run(now=utc(self.base))

        self.assertEqual(set(report.deleted_job_ids), {ready, expired})
        self.assertEqual(self.store.staging_prefix_size(f"jobs/{ready}"), 0)
        self.assertEqual(self.store.staging_prefix_size(f"jobs/{expired}"), 0)
        self.assertGreater(self.store.staging_prefix_size(f"jobs/{active}"), 0)
        self.assertGreater(self.store.staging_prefix_size("unrelated"), 0)
        with self.database.connect() as connection:
            source_key = connection.execute(
                """
                SELECT contents.source_object_key
                FROM contents
                JOIN processing_jobs ON processing_jobs.content_id = contents.id
                WHERE processing_jobs.id = ?
                """,
                (active,),
            ).fetchone()[0]
        with self.store.open(source_key) as source:
            self.assertTrue(source.read().startswith(b"source-"))


if __name__ == "__main__":
    unittest.main()
