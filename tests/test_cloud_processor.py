from __future__ import annotations

import hashlib
import io
import json
import math
import shutil
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.cloud_processor import CloudMediaProcessor
from app.database import Database
from app.job_repository import ProcessingJobRepository
from app.media_tools import MediaTools
from app.object_store import FileSystemObjectStore
from app.processing_worker import ProcessingWorker


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class DeterministicTestRenderer:
    def __init__(self, store: FileSystemObjectStore) -> None:
        self.store = store

    def render(
        self,
        job,
        *,
        audio_key: str,
        duration_ms: int,
        seed: int,
        output_dir: Path,
    ):
        with self.store.open_staging(audio_key) as source:
            self.assert_audio(source.read())
        frame_count = math.ceil(duration_ms / 100)
        output_dir.mkdir(parents=True, exist_ok=False)
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
                f"testsrc2=size=480x320:rate=10:duration={frame_count / 10}",
                "-frames:v",
                str(frame_count),
                "-start_number",
                "0",
                "-threads",
                "1",
                str(output_dir / "frame-%06d.png"),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return {
            "width": 480,
            "height": 320,
            "frame_rate": 10,
            "duration_ms": duration_ms,
            "frame_count": frame_count,
            "seed": seed,
        }

    @staticmethod
    def assert_audio(data: bytes) -> None:
        if not data:
            raise AssertionError("normalized audio is empty")


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg integration tools are required")
class CloudProcessorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_processes_source_to_complete_ready_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "pipeline.db")
            database.initialize()
            store = FileSystemObjectStore(
                objects_root=root / "objects",
                staging_root=root / "staging",
            )
            store.initialize()
            tools = MediaTools(
                ffmpeg_binary=FFMPEG or "ffmpeg",
                ffprobe_binary=FFPROBE or "ffprobe",
                timeout_seconds=30,
            )
            source_path = root / "source.wav"
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
                    "sine=frequency=440:duration=0.45",
                    "-c:a",
                    "pcm_s16le",
                    str(source_path),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            source = source_path.read_bytes()
            created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            user_id = uuid.uuid4().hex
            content_id = uuid.uuid4().hex
            job_id = uuid.uuid4().hex
            source_key = f"contents/{content_id}/source/original"
            store.put(source_key, io.BytesIO(source))
            database.create_user_token(
                user_id=user_id,
                token_id=uuid.uuid4().hex,
                token_digest=uuid.uuid4().hex,
                token_hint="pipeline",
                created_at=created_at,
            )
            database.create_uploaded_content(
                content_id=content_id,
                owner_user_id=user_id,
                display_label="声音碎片 #PIPELINE",
                visual_seed=12345,
                source_object_key=source_key,
                source_filename="source.wav",
                source_content_type="audio/wav",
                source_byte_length=len(source),
                source_sha256=hashlib.sha256(source).hexdigest(),
                job_id=job_id,
                media_object_id=uuid.uuid4().hex,
                created_at=created_at,
            )
            processor = CloudMediaProcessor(
                database=database,
                object_store=store,
                media_tools=tools,
                frame_renderer=DeterministicTestRenderer(store),
            )
            repository = ProcessingJobRepository(database)
            worker = ProcessingWorker(
                repository=repository,
                processor=processor,
                worker_id="pipeline-worker",
                lease_seconds=60,
                poll_seconds=0.01,
            )

            self.assertTrue(await worker.run_once())

            content = database.get_content(content_id)
            job = repository.get_for_content(content_id)
            media = database.get_media_objects(content_id)
            self.assertEqual(content["state"], "READY", dict(job))
            self.assertEqual(job["status"], "COMPLETED")
            self.assertEqual(job["stage"], "READY")
            self.assertEqual(
                {row["kind"] for row in media},
                {"SOURCE", "AUDIO", "AUDIO_INDEX", "VIDEO", "MANIFEST", "REPLAY_PARAMS"},
            )
            manifest_row = next(row for row in media if row["kind"] == "MANIFEST")
            with store.open(manifest_row["object_key"]) as source_file:
                manifest = json.load(source_file)
            self.assertEqual(manifest["content_id"], content_id)
            self.assertEqual(manifest["state"], "READY")
            self.assertEqual(manifest["playback"]["profile"], "t5ai-h264-mp3-v1")
            self.assertEqual(store.staging_prefix_size(f"jobs/{job_id}"), 0)


if __name__ == "__main__":
    unittest.main()
