from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.audio_normalizer import AudioNormalizer
from app.database import Database
from app.job_repository import ClaimedJob, ProcessingJobRepository
from app.media_tools import MediaTools
from app.mp3_index import (
    INDEX_HEADER,
    INDEX_MAGIC,
    INDEX_RECORD,
    InvalidAudioIndex,
    Mp3Indexer,
    build_audio_index,
    parse_mp3_frames,
    validate_audio_index,
)
from app.object_store import FileSystemObjectStore


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class Reporter:
    async def stage(self, name: str) -> None:
        pass


def synthetic_frame(*, padding: int = 0) -> bytes:
    header = bytes((0xFF, 0xFB, 0x90 | (padding << 1), 0x00))
    length = 417 + padding
    return header + bytes(length - 4)


class Mp3IndexUnitTests(unittest.TestCase):
    def test_builds_exact_aix1_records_from_complete_frames(self) -> None:
        audio = b"ID3\x04\x00\x00\x00\x00\x00\x00" + synthetic_frame() + synthetic_frame(padding=1)

        frames = parse_mp3_frames(audio)
        index = build_audio_index(frames)
        validated = validate_audio_index(audio, index, authoritative_duration_ms=52)

        magic, version, record_size, count, reserved = INDEX_HEADER.unpack_from(index)
        self.assertEqual((magic, version, record_size, count, reserved), (INDEX_MAGIC, 1, 16, 2, 0))
        first = INDEX_RECORD.unpack_from(index, INDEX_HEADER.size)
        second = INDEX_RECORD.unpack_from(index, INDEX_HEADER.size + INDEX_RECORD.size)
        self.assertEqual(first[:3], (0, 10, 417))
        self.assertEqual(second[:3], (1152, 427, 418))
        self.assertEqual(validated, frames)

    def test_rejects_corrupt_crc_overlap_and_version(self) -> None:
        audio = synthetic_frame() + synthetic_frame(padding=1)
        index = bytearray(build_audio_index(parse_mp3_frames(audio)))

        corrupt_crc = bytearray(index)
        corrupt_crc[-1] ^= 0x01
        with self.assertRaises(InvalidAudioIndex):
            validate_audio_index(audio, bytes(corrupt_crc), authoritative_duration_ms=52)

        overlap = bytearray(index)
        second_offset = INDEX_HEADER.size + INDEX_RECORD.size
        sample, _, length, crc = INDEX_RECORD.unpack_from(overlap, second_offset)
        INDEX_RECORD.pack_into(overlap, second_offset, sample, 100, length, crc)
        with self.assertRaises(InvalidAudioIndex):
            validate_audio_index(audio, bytes(overlap), authoritative_duration_ms=52)

        unsupported = bytearray(index)
        unsupported[4:6] = (2).to_bytes(2, "little")
        with self.assertRaises(InvalidAudioIndex):
            validate_audio_index(audio, bytes(unsupported), authoritative_duration_ms=52)


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg integration tools are required")
class Mp3IndexerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = Database(root / "index.db")
        self.database.initialize()
        self.store = FileSystemObjectStore(
            objects_root=root / "objects",
            staging_root=root / "staging",
        )
        self.store.initialize()
        self.repository = ProcessingJobRepository(self.database)
        self.tools = MediaTools(
            ffmpeg_binary=FFMPEG or "ffmpeg",
            ffprobe_binary=FFPROBE or "ffprobe",
            timeout_seconds=30,
        )
        self.normalizer = AudioNormalizer(
            database=self.database,
            object_store=self.store,
            media_tools=self.tools,
        )
        self.indexer = Mp3Indexer(self.store)
        self.user_id = "index-user"
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.database.create_user_token(
            user_id=self.user_id,
            token_id="index-token",
            token_digest="index-digest",
            token_hint="index",
            created_at=created_at,
        )
        wav_path = root / "source.wav"
        subprocess.run(
            [
                FFMPEG or "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=523:duration=0.6",
                "-c:a",
                "pcm_s16le",
                str(wav_path),
            ],
            check=True,
        )
        payload = wav_path.read_bytes()
        content_id = uuid.uuid4().hex
        job_id = uuid.uuid4().hex
        source_key = f"contents/{content_id}/source/original"
        self.store.put(source_key, io.BytesIO(payload))
        self.database.create_uploaded_content(
            content_id=content_id,
            owner_user_id=self.user_id,
            display_label="声音碎片 #INDEX",
            visual_seed=1,
            source_object_key=source_key,
            source_filename="source.wav",
            source_content_type="audio/wav",
            source_byte_length=len(payload),
            source_sha256="a" * 64,
            job_id=job_id,
            media_object_id=uuid.uuid4().hex,
            created_at=created_at,
        )
        claimed = self.repository.claim_next(
            worker_id="index-worker",
            now=created_at,
            lease_seconds=60,
        )
        assert claimed is not None
        self.job: ClaimedJob = claimed

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_normalized_mp3_produces_self_validating_index_and_metadata(self) -> None:
        normalized = await self.normalizer.normalize(self.job, Reporter())

        indexed = self.indexer.build(
            self.job,
            authoritative_duration_ms=normalized.duration_ms,
        )

        with self.store.open_staging(indexed.audio_key) as source:
            audio = source.read()
        with self.store.open_staging(indexed.index_key) as source:
            index = source.read()
        frames = validate_audio_index(
            audio,
            index,
            authoritative_duration_ms=normalized.duration_ms,
        )
        self.assertEqual(indexed.frame_count, len(frames))
        self.assertEqual(indexed.duration_ms, normalized.duration_ms)
        self.assertLessEqual(indexed.indexed_duration_ms - indexed.duration_ms, 53)
        self.assertEqual(indexed.sample_rate, 44_100)
        self.assertEqual(indexed.bit_rate, 128_000)
        self.assertEqual(indexed.channels, 1)
        self.assertEqual(indexed.audio_sha256, indexed.audio_etag.strip('"'))
        self.assertEqual(indexed.index_sha256, indexed.index_etag.strip('"'))
        self.assertEqual(indexed.index_byte_length, INDEX_HEADER.size + len(frames) * INDEX_RECORD.size)


if __name__ == "__main__":
    unittest.main()
