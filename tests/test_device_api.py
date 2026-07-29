from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import Settings
from app.job_repository import ProcessingJobRepository
from app.main import create_app
from app.package_publisher import ReadyPackagePublisher

from tests.helpers import register_and_login


class Reporter:
    async def stage(self, name: str) -> None:
        pass


class DeviceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(
            base_dir=root,
            storage_dir=root / "storage",
            database_path=root / "storage" / "device.db",
            object_store_dir=root / "storage" / "objects",
            object_staging_dir=root / "storage" / "object-staging",
            trigger_token="trigger-fixed-token",
            playback_token="playback-fixed-token",
        )
        self.app = create_app(self.settings)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        )
        issued = await register_and_login(self.client)
        self.user_id = issued["user_id"]
        self.user_auth = {"Authorization": f"Bearer {issued['token']}"}
        self.trigger_auth = {"Authorization": "Bearer trigger-fixed-token"}
        self.playback_auth = {"Authorization": "Bearer playback-fixed-token"}
        self.content_id, self.audio_payload = await self.seed_ready_content()

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.temporary.cleanup()

    async def seed_ready_content(self) -> tuple[str, bytes]:
        database = self.app.state.database
        store = self.app.state.object_store
        content_id = uuid.uuid4().hex
        job_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        source = b"original-source"
        source_key = f"contents/{content_id}/source/original"
        store.put(source_key, io.BytesIO(source))
        database.create_uploaded_content(
            content_id=content_id,
            owner_user_id=self.user_id,
            display_label="声音碎片 #DEVICE",
            visual_seed=123,
            source_object_key=source_key,
            source_filename="voice.wav",
            source_content_type="audio/wav",
            source_byte_length=len(source),
            source_sha256=hashlib.sha256(source).hexdigest(),
            job_id=job_id,
            media_object_id=uuid.uuid4().hex,
            created_at=created_at,
        )
        database.update_content_duration(content_id, 1150)
        repository = ProcessingJobRepository(database)
        job = repository.claim_next(
            worker_id="device-test-worker",
            now=created_at,
            lease_seconds=60,
        )
        assert job is not None

        audio = b"normalized-mp3-payload-for-device"
        index = b"AIX1-index-payload-for-device"
        video = b"fast-start-h264-mp4-payload-for-device"
        audio_key = f"jobs/{job_id}/normalized/audio.mp3"
        index_key = f"jobs/{job_id}/indexed/audio.idx"
        video_key = f"jobs/{job_id}/video/video.mp4"
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

        store.replace_staging(
            f"jobs/{job_id}/indexed/audio-index.json",
            io.BytesIO(
                json.dumps(
                    {
                        "schema_version": 1,
                        "content_id": content_id,
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
                            "record_count": 4,
                        },
                    }
                ).encode()
            ),
        )
        store.replace_staging(
            f"jobs/{job_id}/video/video.json",
            io.BytesIO(
                json.dumps(
                    {
                        "schema_version": 1,
                        "content_id": content_id,
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
                ).encode()
            ),
        )
        store.replace_staging(
            f"jobs/{job_id}/replay/replay-params.json",
            io.BytesIO(
                json.dumps(
                    {
                        "seed": 7,
                        "durationMs": 1150,
                        "width": 480,
                        "height": 320,
                        "fps": 10,
                        "quality": "medium",
                    }
                ).encode()
            ),
        )
        await ReadyPackagePublisher(
            database=database,
            object_store=store,
        ).publish(job, Reporter())
        return content_id, audio

    async def test_trigger_resolves_complete_compact_content_with_absolute_urls(self) -> None:
        response = await self.client.get(
            f"/c/{self.content_id}",
            headers=self.trigger_auth,
        )

        self.assertEqual(response.status_code, 200, response.text)
        compact = response.json()
        self.assertEqual(compact["content_id"], self.content_id)
        self.assertEqual(compact["state"], "READY")
        self.assertEqual(compact["trigger"]["display_label"], "声音碎片 #DEVICE")
        self.assertEqual(compact["playback"]["profile"], "t5ai-h264-mp3-v1")
        self.assertEqual(
            compact["playback"]["video"]["url"],
            f"http://testserver/api/v1/contents/{self.content_id}/assets/video",
        )
        self.assertEqual(
            compact["playback"]["audio"]["index_url"],
            f"http://testserver/api/v1/contents/{self.content_id}/assets/audio-index",
        )
        self.assertNotIn("owner_user_id", response.text)
        self.assertNotIn("source_object_key", response.text)
        self.assertEqual(response.headers["cache-control"], "private, max-age=31536000, immutable")
        self.assertEqual(response.headers["vary"], "Authorization")
        self.assertEqual(int(response.headers["content-length"]), len(response.content))
        self.assertTrue(response.headers["etag"].startswith('"'))

    async def test_role_matrix_rejects_missing_unknown_user_and_wrong_device(self) -> None:
        compact_url = f"/c/{self.content_id}"
        asset_url = f"/api/v1/contents/{self.content_id}/assets/audio"

        no_auth = await self.client.get(compact_url)
        self.assertEqual(no_auth.status_code, 200)
        self.assertIn("text/html", no_auth.headers["content-type"])
        self.assertEqual(
            (await self.client.get(compact_url, headers={"Authorization": "Bearer unknown"})).status_code,
            401,
        )
        self.assertEqual((await self.client.get(compact_url, headers=self.user_auth)).status_code, 401)
        self.assertEqual((await self.client.get(compact_url, headers=self.playback_auth)).status_code, 403)
        self.assertEqual((await self.client.get(asset_url, headers=self.trigger_auth)).status_code, 403)
        self.assertEqual((await self.client.get(asset_url, headers=self.user_auth)).status_code, 401)

        device_upload = await self.client.post(
            "/api/v1/contents",
            headers=self.playback_auth,
            files={"audio": ("voice.wav", b"device", "audio/wav")},
        )
        self.assertEqual(device_upload.status_code, 401)

    async def test_playback_get_head_range_and_if_range_contract(self) -> None:
        url = f"/api/v1/contents/{self.content_id}/assets/audio"
        full = await self.client.get(url, headers=self.playback_auth)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.content, self.audio_payload)
        self.assertEqual(full.headers["accept-ranges"], "bytes")
        self.assertEqual(full.headers["cache-control"], "private, max-age=31536000, immutable")
        self.assertEqual(full.headers["vary"], "Authorization")
        etag = full.headers["etag"]

        head = await self.client.head(url, headers=self.playback_auth)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        self.assertEqual(head.headers["content-length"], str(len(self.audio_payload)))
        self.assertEqual(head.headers["etag"], etag)

        for range_value, expected in (
            ("bytes=0-3", self.audio_payload[:4]),
            ("bytes=4-", self.audio_payload[4:]),
            ("bytes=-5", self.audio_payload[-5:]),
        ):
            ranged = await self.client.get(
                url,
                headers={**self.playback_auth, "Range": range_value},
            )
            self.assertEqual(ranged.status_code, 206)
            self.assertEqual(ranged.content, expected)
            self.assertEqual(ranged.headers["content-length"], str(len(expected)))

        matched = await self.client.get(
            url,
            headers={**self.playback_auth, "Range": "bytes=1-2", "If-Range": etag},
        )
        mismatched = await self.client.get(
            url,
            headers={**self.playback_auth, "Range": "bytes=1-2", "If-Range": '"other"'},
        )
        self.assertEqual(matched.status_code, 206)
        self.assertEqual(matched.content, self.audio_payload[1:3])
        self.assertEqual(mismatched.status_code, 200)
        self.assertEqual(mismatched.content, self.audio_payload)

        for invalid in ("bytes=999-", "bytes=3-1", "bytes=0-1,3-4", "bytes=-0"):
            response = await self.client.get(
                url,
                headers={**self.playback_auth, "Range": invalid},
            )
            self.assertEqual(response.status_code, 416)
            self.assertEqual(response.headers["content-range"], f"bytes */{len(self.audio_payload)}")
            self.assertEqual(response.content, b"")

    async def test_non_ready_and_corrupt_objects_are_never_served(self) -> None:
        database = self.app.state.database
        store = self.app.state.object_store
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        pending_id = uuid.uuid4().hex
        source_key = f"contents/{pending_id}/source/original"
        store.put(source_key, io.BytesIO(b"pending"))
        database.create_uploaded_content(
            content_id=pending_id,
            owner_user_id=self.user_id,
            display_label="声音碎片 #PENDING",
            visual_seed=1,
            source_object_key=source_key,
            source_filename="pending.wav",
            source_content_type="audio/wav",
            source_byte_length=7,
            source_sha256=hashlib.sha256(b"pending").hexdigest(),
            job_id=uuid.uuid4().hex,
            media_object_id=uuid.uuid4().hex,
            created_at=created_at,
        )
        self.assertEqual(
            (await self.client.get(f"/c/{pending_id}", headers=self.trigger_auth)).status_code,
            404,
        )

        audio_row = database.get_ready_media_object(
            content_id=self.content_id,
            kind="AUDIO",
        )
        assert audio_row is not None
        final_path = self.settings.object_store_dir / audio_row["object_key"]
        final_path.write_bytes(b"corrupt")
        corrupt = await self.client.get(
            f"/api/v1/contents/{self.content_id}/assets/audio",
            headers=self.playback_auth,
        )
        self.assertEqual(corrupt.status_code, 503)
        self.assertNotEqual(corrupt.content, b"corrupt")

    async def test_flash_preview_returns_public_json(self) -> None:
        response = await self.client.get(f"/api/flash/preview/{self.content_id}")

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["content_id"], self.content_id)
        self.assertEqual(data["title"], "声音碎片 #DEVICE")
        self.assertEqual(data["duration_ms"], 1150)
        self.assertFalse(data["on_chain"])
        self.assertIsNone(data["token_id"])
        self.assertEqual(data["editions_count"], 0)
        self.assertEqual(
            data["audio_url"],
            f"http://testserver/preview/{self.content_id}/audio",
        )
        self.assertEqual(
            data["video_url"],
            f"http://testserver/preview/{self.content_id}/video",
        )

        database = self.app.state.database
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO content_chain "
                "(content_id, chain_state, token_id, tx_hash, contract_address) "
                "VALUES (?, 'MINTED', 42, '0xabc', '0x1234567890abcdef1234')",
                (self.content_id,),
            )

        minted = (await self.client.get(f"/api/flash/preview/{self.content_id}")).json()
        self.assertTrue(minted["on_chain"])
        self.assertEqual(minted["token_id"], 42)
        self.assertEqual(minted["tx_hash"], "0xabc")
        self.assertEqual(minted["contract_address"], "0x1234567890abcdef1234")

    async def test_flash_preview_rejects_unknown_and_pending_content(self) -> None:
        self.assertEqual(
            (await self.client.get("/api/flash/preview/not-a-valid-id")).status_code,
            404,
        )
        self.assertEqual(
            (await self.client.get(f"/api/flash/preview/{uuid.uuid4().hex}")).status_code,
            404,
        )

        database = self.app.state.database
        store = self.app.state.object_store
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        pending_id = uuid.uuid4().hex
        source_key = f"contents/{pending_id}/source/original"
        store.put(source_key, io.BytesIO(b"pending"))
        database.create_uploaded_content(
            content_id=pending_id,
            owner_user_id=self.user_id,
            display_label="声音碎片 #PENDING",
            visual_seed=1,
            source_object_key=source_key,
            source_filename="pending.wav",
            source_content_type="audio/wav",
            source_byte_length=7,
            source_sha256=hashlib.sha256(b"pending").hexdigest(),
            job_id=uuid.uuid4().hex,
            media_object_id=uuid.uuid4().hex,
            created_at=created_at,
        )
        response = await self.client.get(f"/api/flash/preview/{pending_id}")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "内容处理中，请稍后再试")


if __name__ == "__main__":
    unittest.main()
