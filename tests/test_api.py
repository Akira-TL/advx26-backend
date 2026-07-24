from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

import httpx

from app.config import Settings
from app.main import create_app


def sample_files() -> dict[str, tuple[str, bytes, str]]:
    wav = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 28
    stl = b"solid sample\n facet normal 0 0 1\n  outer loop\n  endloop\n endfacet\nendsolid sample\n"
    webm = b"\x1aE\xdf\xa3" + b"test-webm-payload"
    return {
        "audio": ("voice.wav", wav, "audio/wav"),
        "model": ("shape.stl", stl, "model/stl"),
        "video": ("clip.webm", webm, "video/webm"),
    }


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        settings = Settings(
            base_dir=root,
            storage_dir=root / "storage",
            database_path=root / "storage" / "test.db",
            object_store_dir=root / "storage" / "objects",
            object_staging_dir=root / "storage" / "object-staging",
            api_token="test-token",
        )
        self.settings = settings
        self.app = create_app(settings)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        )
        self.auth = {"Authorization": "Bearer test-token"}

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.temporary.cleanup()

    async def upload(self) -> httpx.Response:
        metadata = {
            "title": "测试数据包",
            "description": "端到端测试",
            "creator": {"id": "user-1", "name": "测试用户"},
            "created_at": "2026-07-22T12:00:00Z",
            "tags": ["test", "3d"],
            "custom_metadata": {"source": "unittest"},
        }
        return await self.client.post(
            "/api/v1/packages",
            headers=self.auth,
            data={"metadata": json.dumps(metadata, ensure_ascii=False)},
            files=sample_files(),
        )

    async def test_health_and_authentication(self) -> None:
        response = await self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        response = await self.client.get("/api/v1/ready")
        self.assertEqual(response.status_code, 200)

        response = await self.client.post(
            "/api/v1/packages",
            data={"metadata": "{}"},
            files=sample_files(),
        )
        self.assertEqual(response.status_code, 401)

    async def test_readiness_reports_unavailable_object_store(self) -> None:
        response = await self.client.get("/api/v1/ready")
        self.assertEqual(response.status_code, 200)

        shutil.rmtree(self.settings.object_store_dir)
        self.settings.object_store_dir.write_bytes(b"not-a-directory")

        response = await self.client.get("/api/v1/ready")
        self.assertEqual(response.status_code, 503)

    async def test_upload_query_range_bundle_and_delete(self) -> None:
        response = await self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        package_id = created["package_id"]
        self.assertEqual(len(package_id), 32)

        manifest_response = await self.client.get(created["manifest_url"])
        self.assertEqual(manifest_response.status_code, 200)
        manifest = manifest_response.json()
        self.assertEqual(manifest["title"], "测试数据包")
        self.assertEqual(manifest["files"]["video"]["filename"], "video.webm")
        self.assertEqual(len(manifest["files"]["audio"]["sha256"]), 64)

        listing = await self.client.get("/api/v1/packages", params={"tag": "3d"})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["total"], 1)
        self.assertEqual(listing.json()["items"][0]["package_id"], package_id)

        ranged = await self.client.get(
            f"/api/v1/packages/{package_id}/files/video",
            headers={"Range": "bytes=0-3"},
        )
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, b"\x1aE\xdf\xa3")
        self.assertEqual(ranged.headers["content-range"], "bytes 0-3/21")

        unsatisfiable = await self.client.get(
            f"/api/v1/packages/{package_id}/files/video",
            headers={"Range": "bytes=999-1000"},
        )
        self.assertEqual(unsatisfiable.status_code, 416)

        bundle = await self.client.get(f"/api/v1/packages/{package_id}/bundle")
        self.assertEqual(bundle.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"manifest.json", "audio.wav", "model.stl", "video.webm"},
            )

        deleted = await self.client.delete(
            f"/api/v1/packages/{package_id}", headers=self.auth
        )
        self.assertEqual(deleted.status_code, 204)
        missing = await self.client.get(f"/api/v1/packages/{package_id}")
        self.assertEqual(missing.status_code, 404)

    async def test_invalid_media_is_not_committed(self) -> None:
        files = sample_files()
        files["video"] = ("clip.webm", b"not-a-webm", "video/webm")
        metadata = {
            "creator": {"id": "user-1", "name": "测试用户"},
        }
        response = await self.client.post(
            "/api/v1/packages",
            headers=self.auth,
            data={"metadata": json.dumps(metadata)},
            files=files,
        )
        self.assertEqual(response.status_code, 400)
        listing = await self.client.get("/api/v1/packages")
        self.assertEqual(listing.json()["total"], 0)
        self.assertEqual(list(self.app.state.settings.staging_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

