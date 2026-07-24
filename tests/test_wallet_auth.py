from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_hex

from app.config import Settings
from app.main import create_app


PRIVATE_KEY_A = "0x" + "11" * 32
PRIVATE_KEY_B = "0x" + "22" * 32


def _sign(private_key: str, message: str) -> str:
    account = Account.from_key(private_key)
    signed = account.sign_message(encode_defunct(text=message))
    return to_hex(signed.signature)


class WalletAuthTests(unittest.IsolatedAsyncioTestCase):
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

    async def _challenge(self, address: str) -> str:
        response = await self.client.post(
            "/api/v1/auth/wallet/challenge",
            json={"address": address},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["message"]

    async def _login(self, private_key: str) -> dict[str, str]:
        address = Account.from_key(private_key).address
        message = await self._challenge(address)
        signature = _sign(private_key, message)
        response = await self.client.post(
            "/api/v1/auth/wallet/verify",
            json={"address": address, "signature": signature},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_full_login_flow_issues_bound_token(self) -> None:
        address = Account.from_key(PRIVATE_KEY_A).address
        issued = await self._login(PRIVATE_KEY_A)

        self.assertRegex(issued["user_id"], r"^[0-9a-f]{32}$")
        self.assertTrue(issued["token"].startswith("usr_"))
        self.assertEqual(issued["wallet_address"], address)
        self.assertEqual(issued["token_type"], "Bearer")

        me = await self.client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {issued['token']}"},
        )
        self.assertEqual(me.status_code, 200, me.text)
        body = me.json()
        self.assertEqual(body["user_id"], issued["user_id"])
        self.assertEqual(body["wallet_address"], address)

    async def test_same_wallet_returns_same_account_new_token(self) -> None:
        first = await self._login(PRIVATE_KEY_A)
        second = await self._login(PRIVATE_KEY_A)

        self.assertEqual(first["user_id"], second["user_id"])
        self.assertNotEqual(first["token"], second["token"])

    async def test_distinct_wallets_are_isolated_accounts(self) -> None:
        first = await self._login(PRIVATE_KEY_A)
        second = await self._login(PRIVATE_KEY_B)

        self.assertNotEqual(first["user_id"], second["user_id"])
        self.assertNotEqual(first["wallet_address"], second["wallet_address"])

    async def test_signature_from_other_key_rejected(self) -> None:
        address = Account.from_key(PRIVATE_KEY_A).address
        message = await self._challenge(address)
        signature = _sign(PRIVATE_KEY_B, message)
        response = await self.client.post(
            "/api/v1/auth/wallet/verify",
            json={"address": address, "signature": signature},
        )
        self.assertEqual(response.status_code, 401, response.text)

    async def test_verify_without_challenge_rejected(self) -> None:
        address = Account.from_key(PRIVATE_KEY_A).address
        signature = _sign(PRIVATE_KEY_A, "unsolicited message")
        response = await self.client.post(
            "/api/v1/auth/wallet/verify",
            json={"address": address, "signature": signature},
        )
        self.assertEqual(response.status_code, 401, response.text)

    async def test_invalid_address_rejected(self) -> None:
        response = await self.client.post(
            "/api/v1/auth/wallet/challenge",
            json={"address": "0xnothex"},
        )
        self.assertEqual(response.status_code, 422, response.text)


class WalletChallengeExpiryTests(unittest.IsolatedAsyncioTestCase):
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
            wallet_nonce_ttl_seconds=-1,
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

    async def test_expired_challenge_rejected(self) -> None:
        address = Account.from_key(PRIVATE_KEY_A).address
        challenge = await self.client.post(
            "/api/v1/auth/wallet/challenge",
            json={"address": address},
        )
        self.assertEqual(challenge.status_code, 200, challenge.text)
        message = challenge.json()["message"]
        signature = _sign(PRIVATE_KEY_A, message)
        response = await self.client.post(
            "/api/v1/auth/wallet/verify",
            json={"address": address, "signature": signature},
        )
        self.assertEqual(response.status_code, 401, response.text)


if __name__ == "__main__":
    unittest.main()
