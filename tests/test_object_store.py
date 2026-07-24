from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from app.object_store import FileSystemObjectStore, InvalidObjectKey


class FileSystemObjectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = FileSystemObjectStore(
            objects_root=root / "objects",
            staging_root=root / "staging",
        )
        self.store.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_put_open_stat_range_and_delete(self) -> None:
        payload = b"0123456789abcdef"

        stored = self.store.put("contents/content-1/audio.mp3", io.BytesIO(payload))

        self.assertEqual(stored.key, "contents/content-1/audio.mp3")
        self.assertEqual(stored.byte_length, len(payload))
        self.assertEqual(self.store.stat(stored.key), stored)
        with self.store.open(stored.key) as source:
            self.assertEqual(source.read(), payload)
        ranged = b"".join(self.store.iter_range(stored.key, start=4, length=6, chunk_size=2))
        self.assertEqual(ranged, b"456789")
        self.assertTrue(self.store.delete(stored.key))
        self.assertFalse(self.store.delete(stored.key))
        with self.assertRaises(FileNotFoundError):
            self.store.stat(stored.key)

    def test_promote_staging_prefix_moves_complete_tree(self) -> None:
        self.store.put_staging("jobs/job-1/output/video.mp4", io.BytesIO(b"video"))
        self.store.put_staging("jobs/job-1/output/audio.mp3", io.BytesIO(b"audio"))

        self.store.promote_staging(
            "jobs/job-1/output",
            "contents/content-1/output",
        )

        with self.store.open("contents/content-1/output/video.mp4") as source:
            self.assertEqual(source.read(), b"video")
        with self.store.open("contents/content-1/output/audio.mp3") as source:
            self.assertEqual(source.read(), b"audio")
        with self.assertRaises(FileNotFoundError):
            self.store.stat_staging("jobs/job-1/output/video.mp4")

    def test_rejects_absolute_traversal_and_backslash_keys(self) -> None:
        invalid_keys = (
            "",
            "/absolute/path",
            "../escape",
            "contents/../escape",
            "contents//audio.mp3",
            r"contents\escape.mp3",
        )

        for key in invalid_keys:
            with self.subTest(key=key), self.assertRaises(InvalidObjectKey):
                self.store.put(key, io.BytesIO(b"payload"))


if __name__ == "__main__":
    unittest.main()
