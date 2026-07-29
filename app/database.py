from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    wallet_address TEXT,
    email TEXT,
    password_hash TEXT,
    private_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    disabled_at TEXT
);

CREATE TABLE IF NOT EXISTS auth_nonces (
    address TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_digest TEXT NOT NULL UNIQUE,
    token_hint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    disabled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_tokens_user_id ON user_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_user_tokens_active_digest
    ON user_tokens(token_digest) WHERE disabled_at IS NULL;

CREATE TABLE IF NOT EXISTS contents (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (
        state IN ('UPLOADED', 'PROCESSING', 'READY', 'FAILED', 'DELETED')
    ),
    display_label TEXT NOT NULL,
    visual_seed INTEGER NOT NULL,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    source_object_key TEXT NOT NULL UNIQUE,
    source_filename TEXT NOT NULL,
    source_content_type TEXT NOT NULL,
    source_byte_length INTEGER NOT NULL CHECK (source_byte_length >= 0),
    source_sha256 TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ready_at TEXT,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_contents_owner_created
    ON contents(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contents_state_updated
    ON contents(state, updated_at);

CREATE TABLE IF NOT EXISTS processing_jobs (
    id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL UNIQUE REFERENCES contents(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (
        status IN ('QUEUED', 'CLAIMED', 'RETRY', 'COMPLETED', 'FAILED', 'CANCELLED')
    ),
    stage TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    lease_owner TEXT,
    lease_expires_at TEXT,
    error_code TEXT,
    error_message TEXT,
    retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_processing_jobs_claimable
    ON processing_jobs(status, lease_expires_at, created_at);

CREATE TABLE IF NOT EXISTS media_objects (
    id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (
        kind IN ('SOURCE', 'VIDEO', 'AUDIO', 'AUDIO_INDEX', 'MANIFEST', 'REPLAY_PARAMS')
    ),
    object_key TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL,
    byte_length INTEGER NOT NULL CHECK (byte_length >= 0),
    sha256 TEXT NOT NULL,
    etag TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(content_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_media_objects_content_id
    ON media_objects(content_id);

CREATE TABLE IF NOT EXISTS content_chain (
    content_id TEXT PRIMARY KEY REFERENCES contents(id),
    chain_state TEXT NOT NULL DEFAULT 'NONE'
        CHECK(chain_state IN ('NONE','MINTING','MINTED','FAILED')),
    token_id INTEGER,
    tx_hash TEXT,
    contract_address TEXT,
    token_uri TEXT,
    owner_wallet TEXT,
    error_message TEXT,
    minted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS content_editions (
    id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL REFERENCES contents(id),
    token_id INTEGER NOT NULL,
    tx_hash TEXT NOT NULL,
    owner_wallet TEXT NOT NULL,
    token_uri TEXT NOT NULL,
    edition_type TEXT NOT NULL DEFAULT 'CLAIM' CHECK(edition_type IN ('CREATOR','CLAIM')),
    minted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(content_id, token_id)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_column(connection, "users", "wallet_address", "TEXT")
            self._ensure_column(connection, "users", "email", "TEXT")
            self._ensure_column(connection, "users", "password_hash", "TEXT")
            self._ensure_column(connection, "users", "private_key", "TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_wallet_address "
                "ON users(wallet_address) WHERE wallet_address IS NOT NULL"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email "
                "ON users(email) WHERE email IS NOT NULL"
            )
            self._ensure_column(connection, "contents", "error_message", "TEXT")
            self._ensure_column(connection, "processing_jobs", "error_code", "TEXT")
            self._ensure_column(connection, "processing_jobs", "error_message", "TEXT")
            self._ensure_column(
                connection,
                "processing_jobs",
                "retryable",
                "INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1))",
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        existing = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_user_token(
        self,
        *,
        user_id: str,
        token_id: str,
        token_digest: str,
        token_hint: str,
        created_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO users (id, created_at, updated_at) VALUES (?, ?, ?)",
                (user_id, created_at, created_at),
            )
            connection.execute(
                """
                INSERT INTO user_tokens (
                    id, user_id, token_digest, token_hint, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (token_id, user_id, token_digest, token_hint, created_at),
            )

    def get_active_user_by_token_digest(self, token_digest: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT
                    users.id AS user_id,
                    user_tokens.id AS token_id
                FROM user_tokens
                JOIN users ON users.id = user_tokens.user_id
                WHERE user_tokens.token_digest = ?
                  AND user_tokens.disabled_at IS NULL
                  AND users.disabled_at IS NULL
                """,
                (token_digest,),
            ).fetchone()

    def touch_user_token(self, token_id: str, used_at: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE user_tokens SET last_used_at = ? WHERE id = ?",
                (used_at, token_id),
            )

    def create_user_token_for_user(
        self,
        *,
        user_id: str,
        token_id: str,
        token_digest: str,
        token_hint: str,
        created_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_tokens (
                    id, user_id, token_digest, token_hint, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (token_id, user_id, token_digest, token_hint, created_at),
            )

    def get_user_by_wallet(self, wallet_address: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT id AS user_id, wallet_address
                FROM users
                WHERE wallet_address = ? AND disabled_at IS NULL
                """,
                (wallet_address,),
            ).fetchone()

    def get_user(self, user_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT id AS user_id, wallet_address, email
                FROM users
                WHERE id = ? AND disabled_at IS NULL
                """,
                (user_id,),
            ).fetchone()

    def create_email_user(
        self,
        *,
        user_id: str,
        email: str,
        password_hash: str,
        wallet_address: str,
        stored_private_key: str | None,
        created_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO users (id, email, password_hash, wallet_address, private_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    email,
                    password_hash,
                    wallet_address,
                    stored_private_key,
                    created_at,
                    created_at,
                ),
            )

    def get_user_by_email(self, email: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT id AS user_id, email, password_hash
                FROM users
                WHERE email = ? AND disabled_at IS NULL
                """,
                (email,),
            ).fetchone()

    def create_wallet_user(
        self,
        *,
        user_id: str,
        wallet_address: str,
        token_id: str,
        token_digest: str,
        token_hint: str,
        created_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO users (id, wallet_address, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, wallet_address, created_at, created_at),
            )
            connection.execute(
                """
                INSERT INTO user_tokens (
                    id, user_id, token_digest, token_hint, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (token_id, user_id, token_digest, token_hint, created_at),
            )

    def upsert_auth_nonce(
        self,
        *,
        address: str,
        nonce: str,
        message: str,
        created_at: str,
        expires_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO auth_nonces (address, nonce, message, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    nonce = excluded.nonce,
                    message = excluded.message,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (address, nonce, message, created_at, expires_at),
            )

    def consume_auth_nonce(self, address: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT address, nonce, message, expires_at FROM auth_nonces WHERE address = ?",
                (address,),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "DELETE FROM auth_nonces WHERE address = ?",
                    (address,),
                )
            return row

    def create_uploaded_content(
        self,
        *,
        content_id: str,
        owner_user_id: str,
        display_label: str,
        visual_seed: int,
        source_object_key: str,
        source_filename: str,
        source_content_type: str,
        source_byte_length: int,
        source_sha256: str,
        job_id: str,
        media_object_id: str,
        max_attempts: int = 3,
        created_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO contents (
                    id, owner_user_id, state, display_label, visual_seed,
                    source_object_key, source_filename, source_content_type,
                    source_byte_length, source_sha256, created_at, updated_at
                ) VALUES (?, ?, 'UPLOADED', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content_id,
                    owner_user_id,
                    display_label,
                    visual_seed,
                    source_object_key,
                    source_filename,
                    source_content_type,
                    source_byte_length,
                    source_sha256,
                    created_at,
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO processing_jobs (
                    id, content_id, status, stage, max_attempts, created_at, updated_at
                ) VALUES (?, ?, 'QUEUED', 'UPLOADED', ?, ?, ?)
                """,
                (job_id, content_id, max_attempts, created_at, created_at),
            )
            connection.execute(
                """
                INSERT INTO media_objects (
                    id, content_id, kind, object_key, content_type,
                    byte_length, sha256, etag, created_at
                ) VALUES (?, ?, 'SOURCE', ?, ?, ?, ?, ?, ?)
                """,
                (
                    media_object_id,
                    content_id,
                    source_object_key,
                    source_content_type,
                    source_byte_length,
                    source_sha256,
                    f'sha256-{source_sha256}',
                    created_at,
                ),
            )

    def update_content_duration(self, content_id: str, duration_ms: int) -> bool:
        if duration_ms <= 0:
            raise ValueError("duration_ms must be positive")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE contents
                SET duration_ms = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND state NOT IN ('READY', 'DELETED')
                """,
                (duration_ms, content_id),
            )
            return cursor.rowcount == 1

    def get_content(self, content_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT
                    contents.*,
                    processing_jobs.id AS job_id,
                    processing_jobs.status AS processing_status,
                    processing_jobs.stage AS processing_stage
                FROM contents
                LEFT JOIN processing_jobs ON processing_jobs.content_id = contents.id
                WHERE contents.id = ? AND contents.state != 'DELETED'
                """,
                (content_id,),
            ).fetchone()

    def publish_ready_content(
        self,
        *,
        content_id: str,
        duration_ms: int,
        media_objects: list[dict[str, Any]],
        ready_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            content = connection.execute(
                "SELECT * FROM contents WHERE id = ? AND state != 'DELETED'",
                (content_id,),
            ).fetchone()
            if content is None:
                raise ValueError("content does not exist")
            if content["duration_ms"] != duration_ms:
                raise ValueError("content duration changed before publication")

            for media_object in media_objects:
                existing = connection.execute(
                    "SELECT * FROM media_objects WHERE content_id = ? AND kind = ?",
                    (content_id, media_object["kind"]),
                ).fetchone()
                comparable = (
                    media_object["object_key"],
                    media_object["content_type"],
                    media_object["byte_length"],
                    media_object["sha256"],
                    media_object["etag"],
                )
                if existing is not None:
                    current = (
                        existing["object_key"],
                        existing["content_type"],
                        existing["byte_length"],
                        existing["sha256"],
                        existing["etag"],
                    )
                    if current != comparable:
                        raise ValueError("published media object is immutable")
                    continue
                connection.execute(
                    """
                    INSERT INTO media_objects (
                        id, content_id, kind, object_key, content_type,
                        byte_length, sha256, etag, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        media_object["id"],
                        content_id,
                        media_object["kind"],
                        media_object["object_key"],
                        media_object["content_type"],
                        media_object["byte_length"],
                        media_object["sha256"],
                        media_object["etag"],
                        ready_at,
                    ),
                )

            connection.execute(
                """
                UPDATE contents
                SET state = 'READY',
                    error_code = NULL,
                    error_message = NULL,
                    ready_at = COALESCE(ready_at, ?),
                    updated_at = ?
                WHERE id = ? AND state != 'DELETED'
                """,
                (ready_at, ready_at, content_id),
            )

    def get_media_objects(self, content_id: str) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM media_objects WHERE content_id = ? ORDER BY kind",
                (content_id,),
            ).fetchall()

    def get_ready_media_object(
        self,
        *,
        content_id: str,
        kind: str,
    ) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT media_objects.*
                FROM media_objects
                JOIN contents ON contents.id = media_objects.content_id
                WHERE media_objects.content_id = ?
                  AND media_objects.kind = ?
                  AND contents.state = 'READY'
                  AND contents.deleted_at IS NULL
                """,
                (content_id, kind),
            ).fetchone()

    def retry_owned_content(
        self,
        *,
        owner_user_id: str,
        content_id: str,
        updated_at: str,
    ) -> str | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT
                    contents.state,
                    processing_jobs.status,
                    processing_jobs.retryable
                FROM contents
                JOIN processing_jobs ON processing_jobs.content_id = contents.id
                WHERE contents.id = ? AND contents.owner_user_id = ?
                """,
                (content_id, owner_user_id),
            ).fetchone()
            if row is None:
                return None
            if row["state"] != "FAILED" or row["status"] != "FAILED":
                return "NOT_FAILED"
            if not row["retryable"]:
                return "TERMINAL"
            connection.execute(
                """
                UPDATE processing_jobs
                SET status = 'RETRY',
                    stage = 'UPLOADED',
                    attempt = 0,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    error_code = NULL,
                    error_message = NULL,
                    retryable = 0,
                    last_error = NULL,
                    updated_at = ?,
                    started_at = NULL,
                    finished_at = NULL
                WHERE content_id = ?
                """,
                (updated_at, content_id),
            )
            connection.execute(
                """
                UPDATE contents
                SET state = 'PROCESSING',
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (updated_at, content_id),
            )
            return "RETRIED"

    def delete_owned_content(
        self,
        *,
        owner_user_id: str,
        content_id: str,
        deleted_at: str,
    ) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM contents WHERE id = ? AND owner_user_id = ?",
                (content_id, owner_user_id),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """
                UPDATE contents
                SET state = 'DELETED', deleted_at = COALESCE(deleted_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (deleted_at, deleted_at, content_id),
            )
            connection.execute(
                """
                UPDATE processing_jobs
                SET status = CASE
                        WHEN status IN ('QUEUED', 'CLAIMED', 'RETRY') THEN 'CANCELLED'
                        ELSE status
                    END,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE content_id = ?
                """,
                (deleted_at, content_id),
            )
            return True

    def rename_owned_content(
        self,
        *,
        owner_user_id: str,
        content_id: str,
        display_label: str,
        updated_at: str,
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE contents
                SET display_label = ?, updated_at = ?
                WHERE id = ? AND owner_user_id = ? AND state != 'DELETED'
                """,
                (display_label, updated_at, content_id, owner_user_id),
            )
            return cursor.rowcount > 0

    def list_staging_cleanup_jobs(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT
                    processing_jobs.id AS job_id,
                    processing_jobs.status,
                    processing_jobs.updated_at,
                    processing_jobs.lease_expires_at,
                    contents.state AS content_state
                FROM processing_jobs
                JOIN contents ON contents.id = processing_jobs.content_id
                ORDER BY processing_jobs.updated_at, processing_jobs.id
                """
            ).fetchall()

    def list_owned_contents(self, owner_user_id: str) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT
                    contents.*,
                    processing_jobs.stage AS processing_stage,
                    processing_jobs.status AS processing_status
                FROM contents
                LEFT JOIN processing_jobs ON processing_jobs.content_id = contents.id
                WHERE contents.owner_user_id = ?
                ORDER BY contents.created_at DESC
                """,
                (owner_user_id,),
            ).fetchall()

    def count_owned_contents(self, owner_user_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM contents WHERE owner_user_id = ?",
                (owner_user_id,),
            ).fetchone()
            return int(row["total"])

    def list_owned_contents_page(
        self,
        owner_user_id: str,
        *,
        limit: int,
        offset: int,
    ) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT
                    contents.*,
                    processing_jobs.stage AS processing_stage,
                    processing_jobs.status AS processing_status
                FROM contents
                LEFT JOIN processing_jobs ON processing_jobs.content_id = contents.id
                WHERE contents.owner_user_id = ?
                ORDER BY contents.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (owner_user_id, limit, offset),
            ).fetchall()

    def get_owned_content(
        self,
        *,
        owner_user_id: str,
        content_id: str,
    ) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT
                    contents.*,
                    processing_jobs.stage AS processing_stage,
                    processing_jobs.status AS processing_status
                FROM contents
                LEFT JOIN processing_jobs ON processing_jobs.content_id = contents.id
                WHERE contents.id = ?
                  AND contents.owner_user_id = ?
                """,
                (content_id, owner_user_id),
            ).fetchone()

    def check(self) -> None:
        with self.connect() as connection:
            connection.execute("SELECT 1").fetchone()
