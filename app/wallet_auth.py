from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address

from .auth import UserTokenService
from .database import Database


ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")


class WalletAuthError(Exception):
    """Base error for wallet authentication failures."""


class InvalidWalletAddress(WalletAuthError):
    pass


class ChallengeNotFound(WalletAuthError):
    pass


class ChallengeExpired(WalletAuthError):
    pass


class SignatureMismatch(WalletAuthError):
    pass


@dataclass(frozen=True, slots=True)
class WalletChallenge:
    address: str
    message: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class WalletLogin:
    user_id: str
    wallet_address: str
    token: str


class WalletAuthService:
    def __init__(
        self,
        *,
        database: Database,
        user_tokens: UserTokenService,
        domain: str,
        nonce_ttl_seconds: int,
    ) -> None:
        self.database = database
        self.user_tokens = user_tokens
        self.domain = domain
        self.nonce_ttl_seconds = nonce_ttl_seconds

    def normalize_address(self, address: str) -> str:
        candidate = (address or "").strip()
        if not ADDRESS_PATTERN.fullmatch(candidate):
            raise InvalidWalletAddress("钱包地址格式无效")
        return to_checksum_address(candidate)

    def build_challenge(self, address: str) -> WalletChallenge:
        wallet_address = self.normalize_address(address)
        nonce = secrets.token_urlsafe(24)
        now = _utc_now()
        expires_at = now + timedelta(seconds=self.nonce_ttl_seconds)
        message = self._compose_message(wallet_address, nonce, now)
        self.database.upsert_auth_nonce(
            address=wallet_address,
            nonce=nonce,
            message=message,
            created_at=_iso(now),
            expires_at=_iso(expires_at),
        )
        return WalletChallenge(
            address=wallet_address,
            message=message,
            expires_at=_iso(expires_at),
        )

    def verify(self, address: str, signature: str) -> WalletLogin:
        wallet_address = self.normalize_address(address)
        row = self.database.consume_auth_nonce(wallet_address)
        if row is None:
            raise ChallengeNotFound("请先获取登录挑战")
        if _parse_iso(row["expires_at"]) < _utc_now():
            raise ChallengeExpired("登录挑战已过期，请重新获取")
        message = row["message"]
        try:
            recovered = Account.recover_message(
                encode_defunct(text=message),
                signature=signature,
            )
        except Exception as error:  # noqa: BLE001 - normalize any recovery failure
            raise SignatureMismatch("签名无效") from error
        if to_checksum_address(recovered) != wallet_address:
            raise SignatureMismatch("签名与地址不匹配")

        existing = self.database.get_user_by_wallet(wallet_address)
        if existing is not None:
            issued = self.user_tokens.issue_for_user(existing["user_id"])
        else:
            issued = self.user_tokens.create_wallet_account(wallet_address)
        return WalletLogin(
            user_id=issued.user_id,
            wallet_address=wallet_address,
            token=issued.token,
        )

    def _compose_message(self, address: str, nonce: str, issued_at: datetime) -> str:
        return (
            f"{self.domain} 请求你使用 Injective 钱包登录。\n\n"
            f"钱包地址: {address}\n"
            f"Nonce: {nonce}\n"
            f"签发时间: {_iso(issued_at)}"
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
