from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .database import Database


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job_id: str
    content_id: str
    worker_id: str
    attempt: int
    max_attempts: int
    source_object_key: str


class ProcessingJobRepository:
    """Durable SQLite job state with exclusive lease-based claiming."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def claim_next(
        self,
        *,
        worker_id: str,
        now: str,
        lease_seconds: float,
    ) -> ClaimedJob | None:
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        lease_expires_at = _add_seconds(now, lease_seconds)

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired_claims(connection, now)
            row = connection.execute(
                """
                SELECT
                    processing_jobs.id AS job_id,
                    processing_jobs.content_id,
                    processing_jobs.attempt,
                    processing_jobs.max_attempts,
                    contents.source_object_key
                FROM processing_jobs
                JOIN contents ON contents.id = processing_jobs.content_id
                WHERE processing_jobs.status IN ('QUEUED', 'RETRY')
                  AND processing_jobs.attempt < processing_jobs.max_attempts
                  AND contents.state NOT IN ('READY', 'FAILED', 'DELETED')
                ORDER BY processing_jobs.created_at, processing_jobs.id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None

            next_attempt = int(row["attempt"]) + 1
            cursor = connection.execute(
                """
                UPDATE processing_jobs
                SET status = 'CLAIMED',
                    attempt = ?,
                    lease_owner = ?,
                    lease_expires_at = ?,
                    error_code = NULL,
                    error_message = NULL,
                    retryable = 0,
                    last_error = NULL,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?,
                    finished_at = NULL
                WHERE id = ?
                  AND status IN ('QUEUED', 'RETRY')
                  AND attempt = ?
                """,
                (
                    next_attempt,
                    worker_id,
                    lease_expires_at,
                    now,
                    now,
                    row["job_id"],
                    row["attempt"],
                ),
            )
            if cursor.rowcount != 1:
                return None
            connection.execute(
                """
                UPDATE contents
                SET state = 'PROCESSING',
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = ?
                WHERE id = ? AND state NOT IN ('READY', 'FAILED', 'DELETED')
                """,
                (now, row["content_id"]),
            )

        return ClaimedJob(
            job_id=row["job_id"],
            content_id=row["content_id"],
            worker_id=worker_id,
            attempt=next_attempt,
            max_attempts=int(row["max_attempts"]),
            source_object_key=row["source_object_key"],
        )

    def report_stage(
        self,
        job: ClaimedJob,
        *,
        stage: str,
        now: str,
        lease_seconds: float,
    ) -> bool:
        lease_expires_at = _add_seconds(now, lease_seconds)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE processing_jobs
                SET stage = ?, updated_at = ?, lease_expires_at = ?
                WHERE id = ? AND status = 'CLAIMED' AND lease_owner = ?
                """,
                (stage, now, lease_expires_at, job.job_id, job.worker_id),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                UPDATE contents
                SET state = 'PROCESSING',
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = ?
                WHERE id = ? AND state NOT IN ('READY', 'FAILED', 'DELETED')
                """,
                (now, job.content_id),
            )
        return True

    def renew_lease(
        self,
        job: ClaimedJob,
        *,
        now: str,
        lease_seconds: float,
    ) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE processing_jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'CLAIMED' AND lease_owner = ?
                """,
                (
                    _add_seconds(now, lease_seconds),
                    now,
                    job.job_id,
                    job.worker_id,
                ),
            )
            return cursor.rowcount == 1

    def complete(self, job: ClaimedJob, *, now: str) -> bool:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE processing_jobs
                SET status = 'COMPLETED',
                    stage = 'READY',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    error_code = NULL,
                    error_message = NULL,
                    retryable = 0,
                    last_error = NULL,
                    updated_at = ?,
                    finished_at = ?
                WHERE id = ? AND status = 'CLAIMED' AND lease_owner = ?
                """,
                (now, now, job.job_id, job.worker_id),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                UPDATE contents
                SET state = 'READY',
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = ?,
                    ready_at = ?
                WHERE id = ? AND state != 'DELETED'
                """,
                (now, now, job.content_id),
            )
        return True

    def fail(
        self,
        job: ClaimedJob,
        *,
        code: str,
        message: str,
        retryable: bool,
        now: str,
    ) -> bool:
        should_retry = retryable and job.attempt < job.max_attempts
        status = "RETRY" if should_retry else "FAILED"
        content_state = "PROCESSING" if should_retry else "FAILED"
        finished_at = None if should_retry else now

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE processing_jobs
                SET status = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    error_code = ?,
                    error_message = ?,
                    retryable = ?,
                    last_error = ?,
                    updated_at = ?,
                    finished_at = ?
                WHERE id = ? AND status = 'CLAIMED' AND lease_owner = ?
                """,
                (
                    status,
                    code,
                    message,
                    1 if retryable else 0,
                    message,
                    now,
                    finished_at,
                    job.job_id,
                    job.worker_id,
                ),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                UPDATE contents
                SET state = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE id = ? AND state != 'DELETED'
                """,
                (content_state, code, message, now, job.content_id),
            )
        return True

    def get_for_content(self, content_id: str) -> sqlite3.Row | None:
        with self.database.connect() as connection:
            return connection.execute(
                """
                SELECT
                    processing_jobs.*,
                    contents.state AS content_state,
                    contents.error_code,
                    contents.error_message
                FROM processing_jobs
                JOIN contents ON contents.id = processing_jobs.content_id
                WHERE processing_jobs.content_id = ?
                """,
                (content_id,),
            ).fetchone()

    @staticmethod
    def _recover_expired_claims(
        connection: sqlite3.Connection,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE processing_jobs
            SET status = 'FAILED',
                stage = 'FAILED',
                lease_owner = NULL,
                lease_expires_at = NULL,
                error_code = 'WORKER_INTERRUPTED',
                error_message = '媒体处理进程中断且重试次数已用尽',
                retryable = 1,
                last_error = '媒体处理进程中断且重试次数已用尽',
                updated_at = ?,
                finished_at = ?
            WHERE status = 'CLAIMED'
              AND lease_expires_at <= ?
              AND attempt >= max_attempts
            """,
            (now, now, now),
        )
        connection.execute(
            """
            UPDATE contents
            SET state = 'FAILED',
                error_code = 'WORKER_INTERRUPTED',
                error_message = '媒体处理进程中断且重试次数已用尽',
                updated_at = ?
            WHERE id IN (
                SELECT content_id FROM processing_jobs
                WHERE status = 'FAILED'
                  AND error_code = 'WORKER_INTERRUPTED'
                  AND updated_at = ?
            )
              AND state != 'DELETED'
            """,
            (now, now),
        )
        connection.execute(
            """
            UPDATE processing_jobs
            SET status = 'RETRY',
                lease_owner = NULL,
                lease_expires_at = NULL,
                error_code = 'WORKER_INTERRUPTED',
                error_message = '媒体处理进程中断，正在重试',
                retryable = 1,
                last_error = '媒体处理进程中断，正在重试',
                updated_at = ?
            WHERE status = 'CLAIMED'
              AND lease_expires_at <= ?
              AND attempt < max_attempts
            """,
            (now, now),
        )


def _add_seconds(value: str, seconds: float) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    result = parsed.astimezone(timezone.utc) + timedelta(seconds=seconds)
    return result.isoformat().replace("+00:00", "Z")
