from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from eth_account import Account
from eth_utils import to_checksum_address

from .database import Database


USER_TOKEN_PREFIX = "usr_"
DEVICE_ROLE_TRIGGER = "TRIGGER"
DEVICE_ROLE_PLAYBACK = "PLAYBACK"
PBKDF2_ITERATIONS = 240_000


class EmailAlreadyRegistered(Exception):
    """Raised when registering an email that already exists."""


@dataclass(frozen=True, slots=True)
class UserPrincipal:
    user_id: str
    token_id: str


@dataclass(frozen=True, slots=True)
class IssuedUserToken:
    user_id: str
    token: str


@dataclass(frozen=True, slots=True)
class RegisteredEmailUser:
    user_id: str
    wallet_address: str
    private_key: str
    private_key_stored: bool


@dataclass(frozen=True, slots=True)
class DevicePrincipal:
    device_id: str
    role: str


class DeviceTokenService:
    def __init__(self, *, trigger_token: str, playback_token: str) -> None:
        self.trigger_token = trigger_token
        self.playback_token = playback_token

    def check_configured(self) -> None:
        if not self.trigger_token or not self.playback_token:
            raise RuntimeError("Trigger and Playback tokens must be configured")
        if hmac.compare_digest(self.trigger_token, self.playback_token):
            raise RuntimeError("Trigger and Playback tokens must be different")

    def authenticate(self, authorization: str | None) -> DevicePrincipal | None:
        scheme, separator, token = (authorization or "").partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token:
            return None
        trigger_match = bool(self.trigger_token) and hmac.compare_digest(
            token,
            self.trigger_token,
        )
        playback_match = bool(self.playback_token) and hmac.compare_digest(
            token,
            self.playback_token,
        )
        if trigger_match:
            return DevicePrincipal(device_id="trigger-01", role=DEVICE_ROLE_TRIGGER)
        if playback_match:
            return DevicePrincipal(device_id="playback-01", role=DEVICE_ROLE_PLAYBACK)
        return None


class UserTokenService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def register_email(
        self, email: str, password: str, *, store_private_key: bool = False
    ) -> RegisteredEmailUser:
        user_id = uuid.uuid4().hex
        now = _utc_now()
        account = Account.create()
        wallet_address = to_checksum_address(account.address)
        private_key = "0x" + account.key.hex()
        try:
            self.database.create_email_user(
                user_id=user_id,
                email=email,
                password_hash=_hash_password(password),
                wallet_address=wallet_address,
                stored_private_key=private_key if store_private_key else None,
                created_at=now,
            )
        except sqlite3.IntegrityError as error:
            raise EmailAlreadyRegistered("邮箱已被注册") from error
        return RegisteredEmailUser(
            user_id=user_id,
            wallet_address=wallet_address,
            private_key=private_key,
            private_key_stored=store_private_key,
        )

    def login_email(self, email: str, password: str) -> IssuedUserToken | None:
        row = self.database.get_user_by_email(email)
        if row is None or not _verify_password(password, row["password_hash"]):
            return None
        return self.issue_for_user(row["user_id"])

    def issue_for_user(self, user_id: str) -> IssuedUserToken:
        token_id = uuid.uuid4().hex
        token = f"{USER_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        now = _utc_now()
        self.database.create_user_token_for_user(
            user_id=user_id,
            token_id=token_id,
            token_digest=_digest_token(token),
            token_hint=token[-8:],
            created_at=now,
        )
        return IssuedUserToken(user_id=user_id, token=token)

    def create_wallet_account(self, wallet_address: str) -> IssuedUserToken:
        user_id = uuid.uuid4().hex
        token_id = uuid.uuid4().hex
        token = f"{USER_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        now = _utc_now()
        self.database.create_wallet_user(
            user_id=user_id,
            wallet_address=wallet_address,
            token_id=token_id,
            token_digest=_digest_token(token),
            token_hint=token[-8:],
            created_at=now,
        )
        return IssuedUserToken(user_id=user_id, token=token)

    def authenticate(self, authorization: str | None) -> UserPrincipal | None:
        scheme, separator, token = (authorization or "").partition(" ")
        if (
            separator != " "
            or scheme.lower() != "bearer"
            or not token.startswith(USER_TOKEN_PREFIX)
            or len(token) > 128
        ):
            return None
        row = self.database.get_active_user_by_token_digest(_digest_token(token))
        if row is None:
            return None
        self.database.touch_user_token(row["token_id"], _utc_now())
        return UserPrincipal(user_id=row["user_id"], token_id=row["token_id"])


def _digest_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = base64.b64encode(derived).decode("ascii")
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt_b64}${hash_b64}"


def _verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algorithm, iterations_text, salt_b64, hash_b64 = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(derived, expected)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
