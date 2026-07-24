from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from app.config import Settings
from app.main import create_app
from app.mp3_index import INDEX_HEADER, INDEX_MAGIC, INDEX_RECORD


def synthetic_frame(*, padding: int = 0) -> bytes:
    header = bytes((0xFF, 0xFB, 0x90 | (padding << 1), 0x00))
    length = 417 + padding
    return header + bytes(length - 4)


class DebugMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        media_dir = root / "tmp"
        media_dir.mkdir()
        self.audio_payload = (
            b"ID3\x04\x00\x00\x00\x00\x00\x00"
            + synthetic_frame()
            + synthetic_frame(padding=1)
        )
        (media_dir / "monitoring-30s.mp3").write_bytes(self.audio_payload)
        (media_dir / "monitoring-30s.mp4").write_bytes(b"0123456789")
        settings = Settings(
            base_dir=root,
            storage_dir=root / "storage",
            database_path=root / "storage" / "test.db",
            object_store_dir=root / "storage" / "objects",
            object_staging_dir=root / "storage" / "object-staging",
            debug_media_dir=media_dir,
            worker_enabled=False,
        )
        self.app = create_app(settings)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.temporary.cleanup()

    async def test_audio_route_serves_complete_mp3(self) -> None:
        response = await self.client.get("/debug/audio.mp3")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.audio_payload)
        self.assertEqual(response.headers["content-type"], "audio/mpeg")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_audio_index_route_builds_aix1_and_supports_range(self) -> None:
        complete = await self.client.get("/debug/audio.idx")
        ranged = await self.client.get(
            "/debug/audio.idx",
            headers={"Range": "bytes=0-15"},
        )

        self.assertEqual(complete.status_code, 200)
        self.assertEqual(complete.headers["content-type"], "application/octet-stream")
        magic, version, record_size, record_count, reserved = INDEX_HEADER.unpack_from(
            complete.content
        )
        self.assertEqual(
            (magic, version, record_size, record_count, reserved),
            (INDEX_MAGIC, 1, INDEX_RECORD.size, 2, 0),
        )
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, complete.content[:16])
        self.assertEqual(
            ranged.headers["content-range"],
            f"bytes 0-15/{len(complete.content)}",
        )

    async def test_video_route_supports_range_and_head(self) -> None:
        ranged = await self.client.get(
            "/debug/video.mp4",
            headers={"Range": "bytes=2-5"},
        )
        head = await self.client.head("/debug/video.mp4")

        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, b"2345")
        self.assertEqual(ranged.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(ranged.headers["content-type"], "video/mp4")
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        self.assertEqual(head.headers["content-length"], "10")


if __name__ == "__main__":
    unittest.main()
