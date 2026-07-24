from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.database import Database


class DatabaseFoundationTests(unittest.TestCase):
    def test_initialization_is_repeatable_and_creates_domain_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cloud-media.db"
            database = Database(path)

            database.initialize()
            database.initialize()

            with sqlite3.connect(path) as connection:
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

            self.assertTrue(
                {
                    "users",
                    "user_tokens",
                    "contents",
                    "processing_jobs",
                    "media_objects",
                }.issubset(table_names)
            )
            self.assertNotIn("packages", table_names)


if __name__ == "__main__":
    unittest.main()
