from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .database import Database
from .object_store import FileSystemObjectStore


@dataclass(frozen=True, slots=True)
class CleanupReport:
    deleted_job_ids: tuple[str, ...]
    retained_failed_bytes: int


class StagingCleanup:
    def __init__(
        self,
        *,
        database: Database,
        object_store: FileSystemObjectStore,
        failed_retention_seconds: int = 24 * 60 * 60,
        failed_max_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        if failed_retention_seconds < 0:
            raise ValueError("failed_retention_seconds must be non-negative")
        if failed_max_bytes < 0:
            raise ValueError("failed_max_bytes must be non-negative")
        self.database = database
        self.object_store = object_store
        self.failed_retention_seconds = failed_retention_seconds
        self.failed_max_bytes = failed_max_bytes

    def run(self, *, now: str) -> CleanupReport:
        current = _parse_utc(now)
        deleted: list[str] = []
        retained_failed: list[tuple[datetime, str, int]] = []

        for row in self.database.list_staging_cleanup_jobs():
            job_id = row["job_id"]
            prefix = f"jobs/{job_id}"
            status = row["status"]
            content_state = row["content_state"]
            updated_at = _parse_utc(row["updated_at"])
            lease_expires_at = (
                _parse_utc(row["lease_expires_at"])
                if row["lease_expires_at"]
                else None
            )
            if status == "CLAIMED" and lease_expires_at and lease_expires_at > current:
                continue
            if content_state in {"READY", "DELETED"} or status in {"COMPLETED", "CANCELLED"}:
                if self.object_store.delete_staging_prefix(prefix):
                    deleted.append(job_id)
                continue
            if status != "FAILED":
                continue
            age = current - updated_at
            if age >= timedelta(seconds=self.failed_retention_seconds):
                if self.object_store.delete_staging_prefix(prefix):
                    deleted.append(job_id)
                continue
            retained_failed.append(
                (updated_at, job_id, self.object_store.staging_prefix_size(prefix))
            )

        total = sum(item[2] for item in retained_failed)
        for _, job_id, size in sorted(retained_failed):
            if total <= self.failed_max_bytes:
                break
            if self.object_store.delete_staging_prefix(f"jobs/{job_id}"):
                deleted.append(job_id)
                total -= size
        return CleanupReport(
            deleted_job_ids=tuple(deleted),
            retained_failed_bytes=max(0, total),
        )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
