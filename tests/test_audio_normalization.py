from __future__ import annotations

import asyncio
import io
import json
import math
import shutil
import struct
import wave
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.audio_normalizer import AudioNormalizer
from app.database import Database
from app.job_repository import ProcessingJobRepository
from app.media_tools import (
    CommandResult,
    MediaToolTimeout,
    MediaTools,
    SubprocessCommandRunner,
)
from app.object_store import FileSystemObjectStore
from app.processing_worker import TerminalProcessingError


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StageRecorder:
    def __init__(self) -> None:
        self.stages: list[str] = []

    async def stage(self, name: str) -> None:
        self.stages.append(name)


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg integration tools are required")
class AudioNormalizationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "audio.db")
        self.database.initialize()
        self.object_store = FileSystemObjectStore(
            objects_root=self.root / "objects",
            staging_root=self.root / "staging",
        )
        self.object_store.initialize()
        self.repository = ProcessingJobRepository(self.database)
        self.media_tools = MediaTools(
            ffmpeg_binary=FFMPEG or "ffmpeg",
            ffprobe_binary=FFPROBE or "ffprobe",
            timeout_seconds=30,
        )
        self.normalizer = AudioNormalizer(
            database=self.database,
            object_store=self.object_store,
            media_tools=self.media_tools,
        )
        self.user_id = "audio-user"
        self.database.create_user_token(
            user_id=self.user_id,
            token_id="audio-token",
            token_digest="audio-digest",
            token_hint="audio",
            created_at=now(),
        )

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def generate_fixture(
        self,
        suffix: str,
        *,
        duration: float = 0.45,
        channels: int = 1,
    ) -> bytes:
        path = self.root / f"fixture-{uuid.uuid4().hex}{suffix}"
        command = [
            FFMPEG or "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            "-ar",
            "48000",
            "-ac",
            str(channels),
        ]
        if suffix == ".wav":
            command.extend(["-c:a", "pcm_s16le"])
        elif suffix == ".mp3":
            command.extend(["-c:a", "libmp3lame", "-b:a", "96k"])
        elif suffix in {".m4a", ".aac"}:
            command.extend(["-c:a", "aac", "-b:a", "96k"])
        elif suffix in {".ogg", ".webm"}:
            command.extend(["-c:a", "libopus", "-b:a", "96k"])
        else:
            raise AssertionError(suffix)
        command.append(str(path))
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return path.read_bytes()

    def generate_full_scale_wav(self, *, duration: float = 0.25) -> bytes:
        buffer = io.BytesIO()
        sample_count = int(48_000 * duration)
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48_000)
            frames = bytearray()
            for index in range(sample_count):
                value = int(32_000 * math.sin(2 * math.pi * 440 * index / 48_000))
                frames.extend(struct.pack("<h", value))
            output.writeframes(frames)
        return buffer.getvalue()

    def generate_video_without_audio(self) -> bytes:
        path = self.root / f"video-{uuid.uuid4().hex}.mp4"
        subprocess.run(
            [
                FFMPEG or "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:d=0.2",
                "-an",
                "-c:v",
                "mpeg4",
                str(path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return path.read_bytes()

    def seed_and_claim(self, payload: bytes, filename: str):
        content_id = uuid.uuid4().hex
        job_id = uuid.uuid4().hex
        object_key = f"contents/{content_id}/source/original"
        self.object_store.put(object_key, io.BytesIO(payload))
        created_at = now()
        self.database.create_uploaded_content(
            content_id=content_id,
            owner_user_id=self.user_id,
            display_label=f"声音碎片 #{content_id[:4]}",
            visual_seed=1,
            source_object_key=object_key,
            source_filename=filename,
            source_content_type="application/octet-stream",
            source_byte_length=len(payload),
            source_sha256="a" * 64,
            job_id=job_id,
            media_object_id=uuid.uuid4().hex,
            created_at=created_at,
        )
        job = self.repository.claim_next(
            worker_id="audio-worker",
            now=created_at,
            lease_seconds=60,
        )
        self.assertIsNotNone(job)
        return job

    async def test_accepts_common_phone_audio_containers_by_bytes(self) -> None:
        for suffix in (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".webm"):
            with self.subTest(suffix=suffix):
                payload = self.generate_fixture(suffix)
                job = self.seed_and_claim(payload, f"misleading-{suffix[1:]}.bin")
                reporter = StageRecorder()

                result = await self.normalizer.normalize(job, reporter)

                self.assertEqual(reporter.stages, ["PROBING", "NORMALIZING_AUDIO"])
                self.assertEqual(result.channels, 1)
                self.assertEqual(result.sample_rate, 44_100)
                self.assertGreaterEqual(result.duration_ms, 400)
                self.assertLessEqual(result.duration_ms, 500)
                self.assertGreater(result.mp3_byte_length, 0)
                sample_count = result.pcm_byte_length // (2 * result.channels)
                self.assertEqual(
                    round(sample_count * 1000 / 44_100),
                    result.duration_ms,
                )
                with self.object_store.open(job.source_object_key) as source:
                    self.assertEqual(source.read(), payload)
                with self.object_store.open_staging(result.metadata_key) as source:
                    metadata = json.load(source)
                self.assertEqual(metadata["content_id"], job.content_id)
                self.assertEqual(metadata["channels"], 1)

    async def test_stereo_is_preserved_at_two_channels(self) -> None:
        payload = self.generate_fixture(".wav", channels=2)
        job = self.seed_and_claim(payload, "stereo.wav")

        result = await self.normalizer.normalize(job, StageRecorder())

        self.assertEqual(result.channels, 2)
        self.assertEqual(result.pcm_byte_length % 4, 0)

    async def test_multichannel_input_is_downmixed_to_stereo(self) -> None:
        payload = self.generate_fixture(".wav", channels=6)
        job = self.seed_and_claim(payload, "surround.wav")

        result = await self.normalizer.normalize(job, StageRecorder())

        self.assertEqual(result.channels, 2)
        self.assertEqual(result.pcm_byte_length % 4, 0)

    async def test_audio_longer_than_limit_is_truncated_to_thirty_seconds(self) -> None:
        payload = self.generate_fixture(".mp3", duration=31.2)
        job = self.seed_and_claim(payload, "long.mp3")

        result = await self.normalizer.normalize(job, StageRecorder())
        content = self.database.get_owned_content(
            owner_user_id=self.user_id,
            content_id=job.content_id,
        )

        self.assertEqual(result.duration_ms, 30_000)
        self.assertEqual(content["duration_ms"], 30_000)
        self.assertEqual(result.pcm_byte_length, 30 * 44_100 * 2)

    async def test_retry_replaces_private_artifacts_deterministically(self) -> None:
        payload = self.generate_fixture(".wav")
        job = self.seed_and_claim(payload, "retry.wav")

        first = await self.normalizer.normalize(job, StageRecorder())
        second = await self.normalizer.normalize(job, StageRecorder())

        self.assertEqual(first.mp3_sha256, second.mp3_sha256)
        self.assertEqual(first.pcm_sha256, second.pcm_sha256)
        self.assertEqual(first.duration_ms, second.duration_ms)

    async def test_peak_limiter_keeps_pcm_near_minus_one_dbfs(self) -> None:
        payload = self.generate_full_scale_wav()
        job = self.seed_and_claim(payload, "loud.wav")

        result = await self.normalizer.normalize(job, StageRecorder())
        with self.object_store.open_staging(result.pcm_key) as source:
            pcm = source.read()
        peaks = [abs(value[0]) for value in struct.iter_unpack("<h", pcm)]

        self.assertLessEqual(max(peaks), 29_300)
        self.assertGreater(max(peaks), 20_000)

    async def test_media_without_audio_stream_is_terminal(self) -> None:
        payload = self.generate_video_without_audio()
        job = self.seed_and_claim(payload, "video.mp4")

        with self.assertRaises(TerminalProcessingError) as captured:
            await self.normalizer.normalize(job, StageRecorder())

        self.assertEqual(captured.exception.code, "SOURCE_NO_AUDIO")

    async def test_invalid_bytes_are_terminal_and_original_remains(self) -> None:
        payload = b"not-an-audio-file"
        job = self.seed_and_claim(payload, "voice.wav")

        with self.assertRaises(TerminalProcessingError) as captured:
            await self.normalizer.normalize(job, StageRecorder())

        self.assertEqual(captured.exception.code, "SOURCE_UNPARSEABLE")
        with self.object_store.open(job.source_object_key) as source:
            self.assertEqual(source.read(), payload)


class CapturingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, *, timeout_seconds: float) -> CommandResult:
        self.calls.append(tuple(args))
        return CommandResult(stdout=b"", stderr=b"")


class MediaToolsUnitTests(unittest.TestCase):
    def test_normalize_uses_argument_vector_without_shell_command(self) -> None:
        runner = CapturingRunner()
        tools = MediaTools(runner=runner)

        tools.normalize_audio(
            source_path=Path("source with spaces.bin"),
            stream_index=2,
            channels=2,
            mp3_path=Path("output audio.mp3"),
            pcm_path=Path("output audio.pcm"),
        )

        self.assertEqual(len(runner.calls), 1)
        args = runner.calls[0]
        self.assertEqual(args[0], "ffmpeg")
        self.assertIn("source with spaces.bin", args)
        self.assertIn("output audio.mp3", args)
        self.assertIn("output audio.pcm", args)
        self.assertNotIn("sh", args)
        self.assertNotIn("-c", args[:2])

    def test_subprocess_timeout_has_stable_classification(self) -> None:
        runner = SubprocessCommandRunner()
        with self.assertRaises(MediaToolTimeout):
            runner.run(
                [
                    shutil.which("python") or "python",
                    "-c",
                    "import time; time.sleep(1)",
                ],
                timeout_seconds=0.01,
            )


if __name__ == "__main__":
    unittest.main()
