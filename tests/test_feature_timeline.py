from __future__ import annotations

import array
import hashlib
import io
import json
import math
import tempfile
import unittest
from pathlib import Path

from app.feature_timeline import ALGORITHM_VERSION, FeatureTimelineBuilder
from app.job_repository import ClaimedJob
from app.object_store import FileSystemObjectStore


SAMPLE_RATE = 44_100


def pcm_mono(duration_ms: int, sample_fn) -> bytes:
    count = round(duration_ms * SAMPLE_RATE / 1000)
    values = array.array(
        "h",
        (
            max(-32768, min(32767, round(sample_fn(index / SAMPLE_RATE) * 32767)))
            for index in range(count)
        ),
    )
    return values.tobytes()


class FeatureTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = FileSystemObjectStore(
            objects_root=root / "objects",
            staging_root=root / "staging",
        )
        self.store.initialize()
        self.builder = FeatureTimelineBuilder(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self, pcm: bytes, duration_ms: int, *, job_id: str = "job-1"):
        job = ClaimedJob(
            job_id=job_id,
            content_id="content-1",
            worker_id="worker-1",
            attempt=1,
            max_attempts=3,
            source_object_key="contents/content-1/source/original",
        )
        pcm_key = f"jobs/{job_id}/normalized/audio.pcm"
        metadata_key = f"jobs/{job_id}/normalized/audio.json"
        self.store.replace_staging(pcm_key, io.BytesIO(pcm))
        metadata = json.dumps(
            {
                "duration_ms": duration_ms,
                "channels": 1,
                "sample_rate": SAMPLE_RATE,
                "pcm": {"object_key": pcm_key},
            },
            sort_keys=True,
        ).encode()
        self.store.replace_staging(metadata_key, io.BytesIO(metadata))
        result = self.builder.build(job)
        with self.store.open_staging(result.object_key) as source:
            encoded = source.read()
        return result, encoded, json.loads(encoded)

    def test_silence_is_stable_and_covers_every_video_frame(self) -> None:
        result, encoded, timeline = self.build(
            pcm_mono(550, lambda _: 0.0),
            550,
        )

        self.assertEqual(result.algorithm_version, ALGORITHM_VERSION)
        self.assertEqual(result.frame_rate, 10)
        self.assertEqual(result.frame_count, 6)
        self.assertEqual([item["timestampNanos"] for item in timeline["features"]], [0, 100_000_000, 200_000_000, 300_000_000, 400_000_000, 500_000_000])
        self.assertTrue(all(not item["hasSignal"] for item in timeline["features"]))
        self.assertTrue(all(item["volume"] == 0 for item in timeline["features"]))
        self.assertEqual(result.sha256, hashlib.sha256(encoded).hexdigest())

    def test_frequency_bands_and_centroid_follow_tone_frequency(self) -> None:
        low = pcm_mono(800, lambda time: 0.7 * math.sin(2 * math.pi * 100 * time))
        high = pcm_mono(800, lambda time: 0.7 * math.sin(2 * math.pi * 5000 * time))

        _, _, low_timeline = self.build(low, 800, job_id="low")
        _, _, high_timeline = self.build(high, 800, job_id="high")
        low_feature = low_timeline["features"][-1]
        high_feature = high_timeline["features"][-1]

        self.assertGreater(low_feature["bass"], low_feature["treble"])
        self.assertGreater(high_feature["treble"], high_feature["bass"])
        self.assertGreater(high_feature["centroid"], low_feature["centroid"])
        self.assertGreater(high_feature["pitchNormalized"], low_feature["pitchNormalized"])
        self.assertGreater(low_feature["pitchConfidence"], 0)
        self.assertGreater(high_feature["pitchConfidence"], 0)

    def test_impulse_produces_flux_onset_and_transient_decay(self) -> None:
        def impulse(time: float) -> float:
            sample = round(time * SAMPLE_RATE)
            return 1.0 if 9000 <= sample < 9300 else 0.0

        _, _, timeline = self.build(pcm_mono(600, impulse), 600, job_id="impulse")
        features = timeline["features"]

        self.assertGreater(max(item["spectralFlux"] for item in features), 0)
        self.assertGreater(max(item["onset"] for item in features), 0)
        onset_index = max(range(len(features)), key=lambda index: features[index]["onset"])
        self.assertGreaterEqual(features[onset_index]["transient"], features[onset_index]["onset"])

    def test_identical_pcm_is_byte_identical_and_changed_pcm_changes_identity(self) -> None:
        tone = pcm_mono(400, lambda time: 0.5 * math.sin(2 * math.pi * 440 * time))
        changed = pcm_mono(400, lambda time: 0.5 * math.sin(2 * math.pi * 880 * time))

        first, first_bytes, _ = self.build(tone, 400, job_id="repeat")
        second, second_bytes, _ = self.build(tone, 400, job_id="repeat")
        third, third_bytes, _ = self.build(changed, 400, job_id="changed")

        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.timeline_id, second.timeline_id)
        self.assertNotEqual(first.timeline_id, third.timeline_id)
        self.assertNotEqual(first_bytes, third_bytes)


if __name__ == "__main__":
    unittest.main()
