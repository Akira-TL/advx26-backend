from __future__ import annotations

import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path

import httpx

from app.config import Settings
from app.main import create_app


class ContentApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(
            base_dir=root,
            storage_dir=root / "storage",
            database_path=root / "storage" / "cloud-media.db",
            object_store_dir=root / "storage" / "objects",
            object_staging_dir=root / "storage" / "object-staging",
            max_audio_bytes=1024,
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

    async def issue_user(self) -> dict[str, str]:
        email = f"user-{uuid.uuid4().hex}@example.com"
        password = "password12345"
        register = await self.client.post(
            "/api/v1/users",
            json={"email": email, "password": password},
        )
        self.assertEqual(register.status_code, 201, register.text)
        response = await self.client.post(
            "/api/v1/sessions",
            json={"email": email, "password": password},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    @staticmethod
    def auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def test_issue_token_stores_only_digest_and_authenticates(self) -> None:
        issued = await self.issue_user()

        self.assertRegex(issued["user_id"], r"^[0-9a-f]{32}$")
        self.assertTrue(issued["token"].startswith("usr_"))
        self.assertEqual(issued["token_type"], "Bearer")

        with self.app.state.database.connect() as connection:
            row = connection.execute(
                "SELECT token_digest, token_hint FROM user_tokens WHERE user_id = ?",
                (issued["user_id"],),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotEqual(row["token_digest"], issued["token"])
        self.assertNotIn(issued["token"], row["token_digest"])
        self.assertEqual(len(row["token_digest"]), 64)
        self.assertTrue(issued["token"].endswith(row["token_hint"]))

        response = await self.client.get(
            "/api/v1/contents",
            headers=self.auth(issued["token"]),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"items": [], "total": 0})

    async def test_missing_malformed_and_unknown_user_tokens_share_one_error(self) -> None:
        headers = (
            {},
            {"Authorization": "Basic abc"},
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer unknown-token"},
        )

        responses = [
            await self.client.get("/api/v1/contents", headers=value)
            for value in headers
        ]

        self.assertTrue(all(response.status_code == 401 for response in responses))
        self.assertEqual(
            {response.json()["detail"] for response in responses},
            {"无效或缺少用户 Token"},
        )

    async def test_duplicate_email_registration_is_rejected(self) -> None:
        payload = {"email": "dupe@example.com", "password": "password12345"}
        first = await self.client.post("/api/v1/users", json=payload)
        second = await self.client.post("/api/v1/users", json=payload)

        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 409, second.text)

    async def test_login_rejects_wrong_password_and_unknown_email(self) -> None:
        await self.client.post(
            "/api/v1/users",
            json={"email": "known@example.com", "password": "password12345"},
        )

        wrong = await self.client.post(
            "/api/v1/sessions",
            json={"email": "known@example.com", "password": "wrong-password"},
        )
        unknown = await self.client.post(
            "/api/v1/sessions",
            json={"email": "nobody@example.com", "password": "password12345"},
        )

        self.assertEqual(wrong.status_code, 401, wrong.text)
        self.assertEqual(unknown.status_code, 401, unknown.text)

    async def test_password_is_stored_hashed_not_plaintext(self) -> None:
        password = "password12345"
        register = await self.client.post(
            "/api/v1/users",
            json={"email": "hash@example.com", "password": password},
        )
        user_id = register.json()["user_id"]

        with self.app.state.database.connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()

        self.assertIsNotNone(row["password_hash"])
        self.assertNotIn(password, row["password_hash"])
        self.assertTrue(row["password_hash"].startswith("pbkdf2_sha256$"))

    async def test_registration_assigns_wallet_and_does_not_store_private_key_by_default(self) -> None:
        register = await self.client.post(
            "/api/v1/users",
            json={"email": "wallet@example.com", "password": "password12345"},
        )
        self.assertEqual(register.status_code, 201, register.text)
        body = register.json()
        self.assertRegex(body["wallet_address"], r"^0x[0-9a-fA-F]{40}$")
        self.assertRegex(body["private_key"], r"^0x[0-9a-fA-F]{64}$")
        self.assertFalse(body["private_key_stored"])

        with self.app.state.database.connect() as connection:
            row = connection.execute(
                "SELECT wallet_address, private_key FROM users WHERE id = ?",
                (body["user_id"],),
            ).fetchone()
        self.assertEqual(row["wallet_address"], body["wallet_address"])
        self.assertIsNone(row["private_key"])

    async def test_registration_stores_private_key_when_opted_in(self) -> None:
        register = await self.client.post(
            "/api/v1/users",
            json={
                "email": "custody@example.com",
                "password": "password12345",
                "store_private_key": True,
            },
        )
        self.assertEqual(register.status_code, 201, register.text)
        body = register.json()
        self.assertTrue(body["private_key_stored"])

        with self.app.state.database.connect() as connection:
            row = connection.execute(
                "SELECT private_key FROM users WHERE id = ?",
                (body["user_id"],),
            ).fetchone()
        self.assertEqual(row["private_key"], body["private_key"])

    async def test_upload_persists_exact_source_and_creates_owned_job(self) -> None:
        issued = await self.issue_user()
        payload = b"RIFF" + b"source-audio-bytes" * 4

        response = await self.client.post(
            "/api/v1/contents",
            headers=self.auth(issued["token"]),
            files={"audio": ("voice.wav", payload, "audio/wav")},
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        created = response.json()
        self.assertRegex(created["content_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(created["state"], "UPLOADED")
        self.assertEqual(
            created["status_url"],
            f"/api/v1/contents/{created['content_id']}",
        )

        detail = await self.client.get(
            created["status_url"],
            headers=self.auth(issued["token"]),
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        body = detail.json()
        self.assertEqual(body["content_id"], created["content_id"])
        self.assertEqual(body["state"], "UPLOADED")
        self.assertEqual(body["source"]["filename"], "voice.wav")
        self.assertEqual(body["source"]["content_type"], "audio/wav")
        self.assertEqual(body["source"]["byte_length"], len(payload))
        self.assertEqual(body["source"]["sha256"], hashlib.sha256(payload).hexdigest())

        with self.app.state.database.connect() as connection:
            content = connection.execute(
                "SELECT owner_user_id, source_object_key FROM contents WHERE id = ?",
                (created["content_id"],),
            ).fetchone()
            job = connection.execute(
                "SELECT status, stage FROM processing_jobs WHERE content_id = ?",
                (created["content_id"],),
            ).fetchone()
        self.assertEqual(content["owner_user_id"], issued["user_id"])
        self.assertEqual(dict(job), {"status": "QUEUED", "stage": "UPLOADED"})
        with self.app.state.object_store.open(content["source_object_key"]) as source:
            self.assertEqual(source.read(), payload)

    async def test_database_failure_rolls_back_source_object(self) -> None:
        issued = await self.issue_user()
        with self.app.state.database.connect() as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_content_insert
                BEFORE INSERT ON contents
                BEGIN
                    SELECT RAISE(ABORT, 'forced content failure');
                END
                """
            )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=self.app,
                raise_app_exceptions=False,
            ),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/contents",
                headers=self.auth(issued["token"]),
                files={"audio": ("voice.wav", b"rollback-audio", "audio/wav")},
            )

        self.assertEqual(response.status_code, 500)
        with self.app.state.database.connect() as connection:
            content_count = connection.execute(
                "SELECT COUNT(*) FROM contents"
            ).fetchone()[0]
        self.assertEqual(content_count, 0)
        self.assertEqual(
            [path for path in self.settings.object_store_dir.rglob("*") if path.is_file()],
            [],
        )

    async def test_users_can_only_list_and_get_their_own_content(self) -> None:
        first = await self.issue_user()
        second = await self.issue_user()
        created = await self.client.post(
            "/api/v1/contents",
            headers=self.auth(first["token"]),
            files={"audio": ("voice.wav", b"first-user-audio", "audio/wav")},
        )
        content_id = created.json()["content_id"]

        first_listing = await self.client.get(
            "/api/v1/contents",
            headers=self.auth(first["token"]),
        )
        second_listing = await self.client.get(
            "/api/v1/contents",
            headers=self.auth(second["token"]),
        )
        hidden = await self.client.get(
            f"/api/v1/contents/{content_id}",
            headers=self.auth(second["token"]),
        )

        self.assertEqual(first_listing.json()["total"], 1)
        self.assertEqual(first_listing.json()["items"][0]["content_id"], content_id)
        self.assertEqual(second_listing.json(), {"items": [], "total": 0})
        self.assertEqual(hidden.status_code, 404)

    async def test_empty_and_oversized_uploads_leave_no_content_or_source_object(self) -> None:
        issued = await self.issue_user()

        empty = await self.client.post(
            "/api/v1/contents",
            headers=self.auth(issued["token"]),
            files={"audio": ("empty.wav", b"", "audio/wav")},
        )
        oversized = await self.client.post(
            "/api/v1/contents",
            headers=self.auth(issued["token"]),
            files={"audio": ("large.wav", b"x" * 1025, "audio/wav")},
        )
        listing = await self.client.get(
            "/api/v1/contents",
            headers=self.auth(issued["token"]),
        )

        self.assertEqual(empty.status_code, 400)
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(listing.json(), {"items": [], "total": 0})
        self.assertEqual(
            [path for path in self.settings.object_store_dir.rglob("*") if path.is_file()],
            [],
        )


    async def test_rename_owned_content_updates_display_label(self) -> None:
        issued = await self.issue_user()
        created = await self.client.post(
            "/api/v1/contents",
            headers=self.auth(issued["token"]),
            files={"audio": ("voice.wav", b"rename-audio", "audio/wav")},
        )
        content_id = created.json()["content_id"]

        renamed = await self.client.patch(
            f"/api/v1/contents/{content_id}",
            headers=self.auth(issued["token"]),
            json={"display_label": "我的新名字"},
        )

        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["display_label"], "我的新名字")

        detail = await self.client.get(
            f"/api/v1/contents/{content_id}",
            headers=self.auth(issued["token"]),
        )
        self.assertEqual(detail.json()["display_label"], "我的新名字")

    async def test_rename_rejects_other_users_and_missing_token(self) -> None:
        first = await self.issue_user()
        second = await self.issue_user()
        created = await self.client.post(
            "/api/v1/contents",
            headers=self.auth(first["token"]),
            files={"audio": ("voice.wav", b"rename-owner-audio", "audio/wav")},
        )
        content_id = created.json()["content_id"]

        foreign = await self.client.patch(
            f"/api/v1/contents/{content_id}",
            headers=self.auth(second["token"]),
            json={"display_label": "hijack"},
        )
        anonymous = await self.client.patch(
            f"/api/v1/contents/{content_id}",
            json={"display_label": "anon"},
        )

        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(anonymous.status_code, 401)

        detail = await self.client.get(
            f"/api/v1/contents/{content_id}",
            headers=self.auth(first["token"]),
        )
        self.assertNotEqual(detail.json()["display_label"], "hijack")


if __name__ == "__main__":
    unittest.main()
