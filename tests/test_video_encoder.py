from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.job_repository import ClaimedJob
from app.media_tools import CommandResult, MediaTools
from app.object_store import FileSystemObjectStore
from app.processing_worker import TerminalProcessingError
from app.video_encoder import H264VideoEncoder, validate_h264_mp4


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class Reporter:
    def __init__(self) -> None:
        self.stages: list[str] = []

    async def stage(self, name: str) -> None:
        self.stages.append(name)


class CapturingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, *, timeout_seconds: float) -> CommandResult:
        self.calls.append(tuple(args))
        return CommandResult(stdout=b"", stderr=b"")


class VideoEncoderCommandTests(unittest.TestCase):
    def test_encoder_consumes_ordered_png_frames_without_webm(self) -> None:
        runner = CapturingRunner()
        tools = MediaTools(runner=runner)

        tools.encode_h264_png_sequence(
            frame_pattern=Path("frames/frame-%06d.png"),
            frame_count=12,
            output_path=Path("video.mp4"),
        )

        args = runner.calls[0]
        self.assertIn("frames/frame-%06d.png", args)
        self.assertIn("12", args)
        self.assertIn("baseline", args)
        self.assertIn("yuv420p", args)
        crf_index = args.index("-crf")
        self.assertEqual(args[crf_index + 1], "25.6")
        self.assertIn("+faststart", args)
        self.assertIn("open-gop=0:force-cfr=1:repeat-headers=1", args)
        self.assertNotIn("webm", " ".join(args).lower())


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg integration tools are required")
class VideoEncoderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.frames_dir = self.root / "frames"
        self.frames_dir.mkdir()
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
                "testsrc2=size=480x320:rate=10:duration=1.2",
                "-frames:v",
                "12",
                "-start_number",
                "0",
                "-threads",
                "1",
                str(self.frames_dir / "frame-%06d.png"),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.store = FileSystemObjectStore(
            objects_root=self.root / "objects",
            staging_root=self.root / "staging",
        )
        self.store.initialize()
        self.tools = MediaTools(
            ffmpeg_binary=FFMPEG or "ffmpeg",
            ffprobe_binary=FFPROBE or "ffprobe",
            timeout_seconds=30,
        )
        self.encoder = H264VideoEncoder(
            object_store=self.store,
            media_tools=self.tools,
        )

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def job(self, job_id: str) -> ClaimedJob:
        return ClaimedJob(
            job_id=job_id,
            content_id=f"content-{job_id}",
            worker_id="video-worker",
            attempt=1,
            max_attempts=3,
            source_object_key="unused",
        )

    async def test_encodes_valid_deterministic_fast_start_h264(self) -> None:
        reporter = Reporter()
        first = await self.encoder.encode(
            self.job("one"),
            reporter,
            frames_dir=self.frames_dir,
            authoritative_duration_ms=1150,
        )
        second = await self.encoder.encode(
            self.job("two"),
            Reporter(),
            frames_dir=self.frames_dir,
            authoritative_duration_ms=1150,
        )

        self.assertEqual(reporter.stages, ["ENCODING_VIDEO"])
        self.assertEqual(first.frame_count, 12)
        self.assertEqual(first.duration_ms, 1200)
        self.assertEqual(first.width, 480)
        self.assertEqual(first.height, 320)
        self.assertEqual(first.frame_rate, 10)
        self.assertEqual(first.profile, "Constrained Baseline")
        self.assertEqual(first.pixel_format, "yuv420p")
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.etag, f'"{first.sha256}"')

        with self.store.open_staging(first.object_key) as source:
            payload = source.read()
        with self.store.open_staging(first.metadata_key) as source:
            metadata = json.load(source)
        probe_path = self.root / "probe.mp4"
        probe_path.write_bytes(payload)
        probe = self.tools.inspect_video(probe_path)
        duration = validate_h264_mp4(
            payload,
            probe,
            frame_count=12,
            authoritative_duration_ms=1150,
        )
        self.assertEqual(duration, 1200)
        self.assertEqual(metadata["max_keyframe_interval_frames"], 10)
        self.assertTrue(metadata["fast_start"])
        self.assertFalse(metadata["has_b_frames"])

    async def test_rejects_incomplete_frame_sequence_before_encoding(self) -> None:
        (self.frames_dir / "frame-000011.png").unlink()

        with self.assertRaises(TerminalProcessingError) as captured:
            await self.encoder.encode(
                self.job("missing"),
                Reporter(),
                frames_dir=self.frames_dir,
                authoritative_duration_ms=1150,
            )

        self.assertEqual(captured.exception.code, "VIDEO_OUTPUT_INVALID")


if __name__ == "__main__":
    unittest.main()
