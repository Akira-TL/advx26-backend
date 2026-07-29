from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .database import Database
from .job_repository import ClaimedJob
from .object_store import FileSystemObjectStore
from .processing_worker import TerminalProcessingError, TransientProcessingError


PLAYBACK_PROFILE = "t5ai-h264-mp3-v1"


class StageSink(Protocol):
    async def stage(self, name: str) -> None: ...


class InvalidPublication(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PublishedPackage:
    content_id: str
    manifest_key: str
    manifest_sha256: str
    video_key: str
    audio_key: str
    audio_index_key: str
    duration_ms: int
    replay_params_key: str = ""


@dataclass(frozen=True, slots=True)
class _ObjectDescriptor:
    kind: str
    key: str
    content_type: str
    byte_length: int
    sha256: str
    etag: str
    payload: bytes


class ReadyPackagePublisher:
    def __init__(
        self,
        *,
        database: Database,
        object_store: FileSystemObjectStore,
    ) -> None:
        self.database = database
        self.object_store = object_store

    async def publish(
        self,
        job: ClaimedJob,
        reporter: StageSink,
    ) -> PublishedPackage:
        await reporter.stage("VALIDATING")
        try:
            return await asyncio.to_thread(self._publish_sync, job)
        except InvalidPublication as error:
            raise TerminalProcessingError(
                "PUBLICATION_INVALID",
                "媒体包发布校验失败",
            ) from error
        except sqlite3.Error as error:
            raise TransientProcessingError(
                "PUBLICATION_DATABASE_TEMPORARY",
                "媒体包数据库发布暂时失败",
            ) from error
        except OSError as error:
            raise TransientProcessingError(
                "PUBLICATION_STORAGE_TEMPORARY",
                "媒体包对象发布暂时失败",
            ) from error

    def _publish_sync(self, job: ClaimedJob) -> PublishedPackage:
        content = self.database.get_content(job.content_id)
        if content is None or content["job_id"] != job.job_id:
            raise InvalidPublication("content and processing job do not match")
        duration_ms = content["duration_ms"]
        if not isinstance(duration_ms, int) or not 0 < duration_ms <= 30_000:
            raise InvalidPublication("authoritative duration is invalid")

        audio_metadata = self._read_json(
            f"jobs/{job.job_id}/indexed/audio-index.json"
        )
        video_metadata = self._read_json(f"jobs/{job.job_id}/video/video.json")
        audio = self._load_staging_object(
            kind="AUDIO",
            metadata=audio_metadata.get("audio"),
            expected_key=f"jobs/{job.job_id}/normalized/audio.mp3",
            content_type="audio/mpeg",
        )
        audio_index = self._load_staging_object(
            kind="AUDIO_INDEX",
            metadata=audio_metadata.get("index"),
            expected_key=f"jobs/{job.job_id}/indexed/audio.idx",
            content_type="application/octet-stream",
        )
        video = self._load_staging_object(
            kind="VIDEO",
            metadata=video_metadata,
            expected_key=f"jobs/{job.job_id}/video/video.mp4",
            content_type="video/mp4",
        )
        replay_params = self._load_replay_params(
            expected_key=f"jobs/{job.job_id}/replay/replay-params.json",
        )
        self._validate_profiles(
            content_id=job.content_id,
            duration_ms=duration_ms,
            audio_metadata=audio_metadata,
            video_metadata=video_metadata,
        )

        final_prefix = f"contents/{job.content_id}/output"
        manifest = self._build_manifest(
            content_id=job.content_id,
            display_label=content["display_label"],
            duration_ms=duration_ms,
            audio=audio,
            audio_index=audio_index,
            video=video,
            audio_metadata=audio_metadata,
            video_metadata=video_metadata,
        )
        manifest_payload = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
        manifest_object = _ObjectDescriptor(
            kind="MANIFEST",
            key="",
            content_type="application/json",
            byte_length=len(manifest_payload),
            sha256=manifest_sha256,
            etag=f'"{manifest_sha256}"',
            payload=manifest_payload,
        )
        final_objects = {
            "video.mp4": video,
            "audio.mp3": audio,
            "audio.idx": audio_index,
            "replay-params.json": replay_params,
            "manifest.json": manifest_object,
        }
        final_keys = {
            filename: f"{final_prefix}/{filename}" for filename in final_objects
        }

        final_presence = {
            filename: self._object_exists(key)
            for filename, key in final_keys.items()
        }
        if any(final_presence.values()) and not all(final_presence.values()):
            raise InvalidPublication("partial final package already exists")
        if all(final_presence.values()):
            self._verify_final_objects(final_keys, final_objects)
        else:
            publication_prefix = f"jobs/{job.job_id}/publish"
            for filename, descriptor in final_objects.items():
                self.object_store.replace_staging(
                    f"{publication_prefix}/{filename}",
                    io.BytesIO(descriptor.payload),
                )
            try:
                self.object_store.promote_staging(publication_prefix, final_prefix)
            except FileExistsError:
                self._verify_final_objects(final_keys, final_objects)

        ready_at = _utc_now()
        database_objects = []
        for filename, descriptor in final_objects.items():
            final_key = final_keys[filename]
            database_objects.append(
                {
                    "id": uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"advx26:{job.content_id}:{descriptor.kind}",
                    ).hex,
                    "kind": descriptor.kind,
                    "object_key": final_key,
                    "content_type": descriptor.content_type,
                    "byte_length": descriptor.byte_length,
                    "sha256": descriptor.sha256,
                    "etag": descriptor.etag,
                }
            )
        self.database.publish_ready_content(
            content_id=job.content_id,
            duration_ms=duration_ms,
            media_objects=database_objects,
            ready_at=ready_at,
        )
        try:
            self.object_store.delete_staging_prefix(f"jobs/{job.job_id}")
        except OSError:
            pass
        return PublishedPackage(
            content_id=job.content_id,
            manifest_key=final_keys["manifest.json"],
            manifest_sha256=manifest_sha256,
            video_key=final_keys["video.mp4"],
            audio_key=final_keys["audio.mp3"],
            audio_index_key=final_keys["audio.idx"],
            duration_ms=duration_ms,
            replay_params_key=final_keys["replay-params.json"],
        )

    def _read_json(self, key: str) -> dict[str, object]:
        try:
            with self.object_store.open_staging(key) as source:
                payload = json.load(source)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
            raise InvalidPublication(f"missing or invalid metadata: {key}") from error
        if not isinstance(payload, dict):
            raise InvalidPublication(f"metadata is not an object: {key}")
        return payload

    def _load_staging_object(
        self,
        *,
        kind: str,
        metadata: object,
        expected_key: str,
        content_type: str,
    ) -> _ObjectDescriptor:
        if not isinstance(metadata, dict):
            raise InvalidPublication(f"{kind} metadata is missing")
        if metadata.get("object_key") != expected_key:
            raise InvalidPublication(f"{kind} object key mismatch")
        try:
            byte_length = int(metadata["byte_length"])
            sha256 = str(metadata["sha256"])
            etag = str(metadata["etag"])
            with self.object_store.open_staging(expected_key) as source:
                payload = source.read()
        except (KeyError, TypeError, ValueError, FileNotFoundError, OSError) as error:
            raise InvalidPublication(f"{kind} object metadata is invalid") from error
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if (
            len(payload) != byte_length
            or actual_sha256 != sha256
            or etag != f'"{sha256}"'
            or sha256.lower() != sha256
        ):
            raise InvalidPublication(f"{kind} integrity metadata mismatch")
        return _ObjectDescriptor(
            kind=kind,
            key=expected_key,
            content_type=content_type,
            byte_length=byte_length,
            sha256=sha256,
            etag=etag,
            payload=payload,
        )

    def _load_replay_params(self, *, expected_key: str) -> _ObjectDescriptor:
        try:
            with self.object_store.open_staging(expected_key) as source:
                payload = source.read()
            parsed = json.loads(payload)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
            raise InvalidPublication("replay params missing or invalid") from error
        if not isinstance(parsed, dict):
            raise InvalidPublication("replay params is not an object")
        seed = parsed.get("seed")
        duration_ms = parsed.get("durationMs")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise InvalidPublication("replay params seed is invalid")
        if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms <= 0:
            raise InvalidPublication("replay params duration is invalid")
        sha256 = hashlib.sha256(payload).hexdigest()
        return _ObjectDescriptor(
            kind="REPLAY_PARAMS",
            key=expected_key,
            content_type="application/json",
            byte_length=len(payload),
            sha256=sha256,
            etag=f'"{sha256}"',
            payload=payload,
        )

    @staticmethod
    def _validate_profiles(
        *,
        content_id: str,
        duration_ms: int,
        audio_metadata: dict[str, object],
        video_metadata: dict[str, object],
    ) -> None:
        if audio_metadata.get("content_id") != content_id:
            raise InvalidPublication("audio content identity mismatch")
        if video_metadata.get("content_id") != content_id:
            raise InvalidPublication("video content identity mismatch")
        if int(audio_metadata.get("duration_ms", -1)) != duration_ms:
            raise InvalidPublication("audio duration mismatch")
        video_duration = int(video_metadata.get("duration_ms", -1))
        if video_duration < duration_ms or video_duration - duration_ms >= 100:
            raise InvalidPublication("video duration mismatch")
        audio = audio_metadata.get("audio")
        index = audio_metadata.get("index")
        if not isinstance(audio, dict) or not isinstance(index, dict):
            raise InvalidPublication("audio descriptors are missing")
        expected_audio = {
            "sample_rate": 44_100,
            "bit_rate": 128_000,
        }
        if any(audio.get(key) != value for key, value in expected_audio.items()):
            raise InvalidPublication("audio profile mismatch")
        if audio.get("channels") not in {1, 2} or index.get("version") != 1:
            raise InvalidPublication("audio channel or index profile mismatch")
        expected_video = {
            "codec": "h264",
            "profile": "Constrained Baseline",
            "pixel_format": "yuv420p",
            "width": 480,
            "height": 320,
            "frame_rate": 10,
            "has_b_frames": False,
            "fast_start": True,
        }
        if any(video_metadata.get(key) != value for key, value in expected_video.items()):
            raise InvalidPublication("video profile mismatch")
        if int(video_metadata.get("max_keyframe_interval_frames", -1)) > 10:
            raise InvalidPublication("video keyframe interval mismatch")

    @staticmethod
    def _build_manifest(
        *,
        content_id: str,
        display_label: str,
        duration_ms: int,
        audio: _ObjectDescriptor,
        audio_index: _ObjectDescriptor,
        video: _ObjectDescriptor,
        audio_metadata: dict[str, object],
        video_metadata: dict[str, object],
    ) -> dict[str, object]:
        audio_profile = audio_metadata["audio"]
        index_profile = audio_metadata["index"]
        assert isinstance(audio_profile, dict)
        assert isinstance(index_profile, dict)
        base = f"/api/v1/contents/{content_id}/assets"
        return {
            "schema_version": 1,
            "content_id": content_id,
            "state": "READY",
            "duration_ms": duration_ms,
            "trigger": {
                "display_label": display_label,
                "autoplay": True,
                "end_behavior": "HOLD_LAST_FRAME",
                "controls": ["PLAY", "PAUSE", "SEEK", "STOP", "REPLAY"],
            },
            "playback": {
                "profile": PLAYBACK_PROFILE,
                "video": {
                    "url": f"{base}/video",
                    "format": "mp4",
                    "codec": "h264",
                    "profile": "Constrained Baseline",
                    "pixel_format": "yuv420p",
                    "width": 480,
                    "height": 320,
                    "fps": 10,
                    "duration_ms": video_metadata["duration_ms"],
                    "max_keyframe_interval_ms": 1000,
                    "byte_length": video.byte_length,
                    "sha256": video.sha256,
                    "etag": video.etag,
                },
                "audio": {
                    "url": f"{base}/audio",
                    "index_url": f"{base}/audio-index",
                    "format": "mp3",
                    "bitrate": 128_000,
                    "sample_rate": 44_100,
                    "channels": audio_profile["channels"],
                    "byte_length": audio.byte_length,
                    "sha256": audio.sha256,
                    "etag": audio.etag,
                    "index_version": index_profile["version"],
                    "index_byte_length": audio_index.byte_length,
                    "index_sha256": audio_index.sha256,
                    "index_etag": audio_index.etag,
                },
                "replay": {
                    "kind": "webgl-interactive",
                    "url": f"/replay-viewer/replay.html?content={content_id}",
                    "params_url": f"{base}/replay-params",
                },
            },
        }

    def _object_exists(self, key: str) -> bool:
        try:
            self.object_store.stat(key)
            return True
        except FileNotFoundError:
            return False

    def _verify_final_objects(
        self,
        final_keys: dict[str, str],
        descriptors: dict[str, _ObjectDescriptor],
    ) -> None:
        for filename, key in final_keys.items():
            try:
                with self.object_store.open(key) as source:
                    payload = source.read()
            except FileNotFoundError as error:
                raise InvalidPublication("final package is partial") from error
            descriptor = descriptors[filename]
            if (
                len(payload) != descriptor.byte_length
                or hashlib.sha256(payload).hexdigest() != descriptor.sha256
            ):
                raise InvalidPublication("immutable final object mismatch")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
