from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    creator_id TEXT NOT NULL,
    creator_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    custom_metadata_json TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    audio_filename TEXT NOT NULL,
    audio_size INTEGER NOT NULL,
    audio_sha256 TEXT NOT NULL,
    model_filename TEXT NOT NULL,
    model_size INTEGER NOT NULL,
    model_sha256 TEXT NOT NULL,
    video_filename TEXT NOT NULL,
    video_size INTEGER NOT NULL,
    video_sha256 TEXT NOT NULL,
    total_size INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_packages_received_at ON packages(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_packages_creator_id ON packages(creator_id);
CREATE INDEX IF NOT EXISTS idx_packages_status ON packages(status);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

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

    def insert_package(self, values: dict[str, Any]) -> None:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.connect() as connection:
            connection.execute(
                f"INSERT INTO packages ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )

    def get_package(self, package_id: str, include_deleted: bool = False) -> sqlite3.Row | None:
        query = "SELECT * FROM packages WHERE id = ?"
        parameters: list[Any] = [package_id]
        if not include_deleted:
            query += " AND status != 'deleted'"
        with self.connect() as connection:
            return connection.execute(query, parameters).fetchone()

    def list_packages(
        self,
        *,
        limit: int,
        offset: int,
        creator_id: str | None,
        status: str | None,
        tag: str | None,
    ) -> tuple[list[sqlite3.Row], int]:
        clauses = ["status != 'deleted'"]
        parameters: list[Any] = []
        if creator_id:
            clauses.append("creator_id = ?")
            parameters.append(creator_id)
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        if tag:
            clauses.append("tags_json LIKE ? ESCAPE '\\'")
            escaped = tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.append(f'%"{escaped}"%')
        where = " AND ".join(clauses)
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM packages WHERE {where}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM packages WHERE {where} ORDER BY received_at DESC LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
            ).fetchall()
        return rows, total

    def mark_deleted(self, package_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE packages SET status = 'deleted' WHERE id = ? AND status != 'deleted'",
                (package_id,),
            )
            return cursor.rowcount > 0

    def check(self) -> None:
        with self.connect() as connection:
            connection.execute("SELECT 1").fetchone()
