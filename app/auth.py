from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .database import Database


USER_TOKEN_PREFIX = "usr_"


@dataclass(frozen=True, slots=True)
class UserPrincipal:
    user_id: str
    token_id: str


@dataclass(frozen=True, slots=True)
class IssuedUserToken:
    user_id: str
    token: str


class UserTokenService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def issue(self) -> IssuedUserToken:
        user_id = uuid.uuid4().hex
        token_id = uuid.uuid4().hex
        token = f"{USER_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        now = _utc_now()
        self.database.create_user_token(
            user_id=user_id,
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
