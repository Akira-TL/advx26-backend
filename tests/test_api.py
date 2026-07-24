from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import httpx

from app.config import Settings
from app.main import create_app


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(
            base_dir=root,
            storage_dir=root / "storage",
            database_path=root / "storage" / "test.db",
            object_store_dir=root / "storage" / "objects",
            object_staging_dir=root / "storage" / "object-staging",
            trigger_token="trigger-test-token",
            playback_token="playback-test-token",
        )
        self.app = create_app(self.settings)
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

    async def test_health_and_readiness_have_distinct_contracts(self) -> None:
        health = await self.client.get("/api/v1/health")
        ready = await self.client.get("/api/v1/ready")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["status"], "ready")
        self.assertEqual(ready.json()["worker"], "disabled")

    async def test_readiness_reports_unavailable_object_store(self) -> None:
        shutil.rmtree(self.settings.object_store_dir)
        self.settings.object_store_dir.write_bytes(b"not-a-directory")

        response = await self.client.get("/api/v1/ready")

        self.assertEqual(response.status_code, 503)

    async def test_readiness_reports_missing_media_tool(self) -> None:
        self.app.state.media_tools.ffmpeg_binary = "missing-ffmpeg-for-test"

        response = await self.client.get("/api/v1/ready")

        self.assertEqual(response.status_code, 503)

    async def test_openapi_contains_only_new_roles_and_contract(self) -> None:
        schema = (await self.client.get("/openapi.json")).json()
        paths = schema["paths"]
        schemes = schema["components"]["securitySchemes"]

        self.assertIn("/api/v1/users", paths)
        self.assertIn("/api/v1/sessions", paths)
        self.assertIn("/api/v1/contents", paths)
        self.assertIn("/api/v1/contents/{content_id}/retry", paths)
        self.assertIn("/c/{content_id}", paths)
        self.assertIn(
            "/api/v1/contents/{content_id}/assets/{asset_kind}",
            paths,
        )
        self.assertNotIn("/api/v1/packages", paths)
        self.assertEqual(set(schemes), {"UserToken", "TriggerToken", "PlaybackToken"})

    async def test_legacy_package_routes_are_unreachable(self) -> None:
        for method, path in (
            ("post", "/api/v1/packages"),
            ("get", "/api/v1/packages"),
            ("get", "/api/v1/packages/deadbeef"),
            ("get", "/api/v1/packages/deadbeef/bundle"),
        ):
            response = await getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
