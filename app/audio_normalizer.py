from __future__ import annotations

import asyncio
import hashlib
import io
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .database import Database
from .job_repository import ClaimedJob
from .media_tools import (
    MediaCommandFailed,
    MediaProbeFailed,
    MediaToolTimeout,
    MediaToolUnavailable,
    MediaTools,
)
from .object_store import FileSystemObjectStore
from .processing_worker import TerminalProcessingError, TransientProcessingError


SAMPLE_RATE = 44_100
SAMPLE_WIDTH_BYTES = 2


class StageSink(Protocol):
    async def stage(self, name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class NormalizedAudio:
    mp3_key: str
    pcm_key: str
    metadata_key: str
    duration_ms: int
    channels: int
    sample_rate: int
    mp3_byte_length: int
    pcm_byte_length: int
    mp3_sha256: str
    pcm_sha256: str


@dataclass(frozen=True, slots=True)
class _PreparedSource:
    source_path: Path
    stream_index: int
    channels: int


class AudioNormalizer:
    """Normalize one claimed source object into private deterministic audio artifacts."""

    def __init__(
        self,
        *,
        database: Database,
        object_store: FileSystemObjectStore,
        media_tools: MediaTools,
    ) -> None:
        self.database = database
        self.object_store = object_store
        self.media_tools = media_tools

    async def normalize(
        self,
        job: ClaimedJob,
        reporter: StageSink,
    ) -> NormalizedAudio:
        try:
            with tempfile.TemporaryDirectory(prefix="cloud-audio-") as directory:
                work_dir = Path(directory)
                await reporter.stage("PROBING")
                prepared = await asyncio.to_thread(
                    self._prepare_source,
                    job,
                    work_dir,
                )
                await reporter.stage("NORMALIZING_AUDIO")
                return await asyncio.to_thread(
                    self._normalize_prepared,
                    job,
                    prepared,
                    work_dir,
                )
        except MediaProbeFailed as error:
            raise TerminalProcessingError(
                "SOURCE_UNPARSEABLE",
                "音频文件无法解析",
            ) from error
        except MediaToolUnavailable as error:
            raise TransientProcessingError(
                "MEDIA_TOOL_UNAVAILABLE",
                "媒体处理工具暂不可用",
            ) from error
        except MediaToolTimeout as error:
            raise TransientProcessingError(
                "MEDIA_TOOL_TIMEOUT",
                "媒体处理超时",
            ) from error
        except MediaCommandFailed as error:
            if _looks_like_tool_configuration_error(error.diagnostic):
                raise TransientProcessingError(
                    "MEDIA_TOOL_UNAVAILABLE",
                    "媒体处理工具暂不可用",
                ) from error
            raise TerminalProcessingError(
                "SOURCE_DECODE_FAILED",
                "音频解码或格式修复失败",
            ) from error
        except (TerminalProcessingError, TransientProcessingError):
            raise
        except OSError as error:
            raise TransientProcessingError(
                "AUDIO_STORAGE_TEMPORARY",
                "音频暂存失败",
            ) from error

    def _prepare_source(
        self,
        job: ClaimedJob,
        work_dir: Path,
    ) -> _PreparedSource:
        source_path = work_dir / "source.upload"
        with self.object_store.open(job.source_object_key) as source:
            with source_path.open("xb") as destination:
                shutil.copyfileobj(source, destination)

        probe = self.media_tools.probe(source_path)
        audio_streams = [
            stream for stream in probe.streams if stream.codec_type == "audio"
        ]
        if not audio_streams:
            raise TerminalProcessingError(
                "SOURCE_NO_AUDIO",
                "上传内容不包含音频轨道",
            )
        if all(stream.encrypted for stream in audio_streams):
            raise TerminalProcessingError(
                "SOURCE_ENCRYPTED",
                "暂不支持加密音频",
            )
        stream = probe.first_audio_stream()
        if stream is None or not stream.channels or stream.channels < 1:
            raise TerminalProcessingError(
                "SOURCE_UNSUPPORTED",
                "音频编码或声道布局不受支持",
            )
        return _PreparedSource(
            source_path=source_path,
            stream_index=stream.index,
            channels=1 if stream.channels == 1 else 2,
        )

    def _normalize_prepared(
        self,
        job: ClaimedJob,
        prepared: _PreparedSource,
        work_dir: Path,
    ) -> NormalizedAudio:
        prefix = f"jobs/{job.job_id}/normalized"
        mp3_key = f"{prefix}/audio.mp3"
        pcm_key = f"{prefix}/audio.pcm"
        metadata_key = f"{prefix}/audio.json"
        mp3_path = work_dir / "audio.mp3"
        pcm_path = work_dir / "audio.pcm"

        self.media_tools.normalize_audio(
            source_path=prepared.source_path,
            stream_index=prepared.stream_index,
            channels=prepared.channels,
            mp3_path=mp3_path,
            pcm_path=pcm_path,
        )
        result = self._validate_outputs(
            job=job,
            mp3_path=mp3_path,
            pcm_path=pcm_path,
            mp3_key=mp3_key,
            pcm_key=pcm_key,
            metadata_key=metadata_key,
            channels=prepared.channels,
        )

        with mp3_path.open("rb") as source:
            self.object_store.replace_staging(mp3_key, source)
        with pcm_path.open("rb") as source:
            self.object_store.replace_staging(pcm_key, source)
        metadata = json.dumps(
            {
                "schema_version": 1,
                "content_id": job.content_id,
                "duration_ms": result.duration_ms,
                "channels": result.channels,
                "sample_rate": result.sample_rate,
                "mp3": {
                    "object_key": result.mp3_key,
                    "byte_length": result.mp3_byte_length,
                    "sha256": result.mp3_sha256,
                },
                "pcm": {
                    "object_key": result.pcm_key,
                    "byte_length": result.pcm_byte_length,
                    "sha256": result.pcm_sha256,
                    "sample_format": "S16LE",
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.object_store.replace_staging(metadata_key, io.BytesIO(metadata))
        if not self.database.update_content_duration(job.content_id, result.duration_ms):
            raise OSError("content disappeared while normalizing audio")
        return result

    def _validate_outputs(
        self,
        *,
        job: ClaimedJob,
        mp3_path: Path,
        pcm_path: Path,
        mp3_key: str,
        pcm_key: str,
        metadata_key: str,
        channels: int,
    ) -> NormalizedAudio:
        if not mp3_path.is_file() or not pcm_path.is_file():
            raise TransientProcessingError(
                "AUDIO_OUTPUT_MISSING",
                "音频处理结果不完整",
            )
        pcm_byte_length = pcm_path.stat().st_size
        frame_bytes = SAMPLE_WIDTH_BYTES * channels
        if pcm_byte_length <= 0 or pcm_byte_length % frame_bytes != 0:
            raise TerminalProcessingError(
                "SOURCE_DECODE_FAILED",
                "音频解码结果为空或损坏",
            )
        sample_count = pcm_byte_length // frame_bytes
        duration_ms = round(sample_count * 1000 / SAMPLE_RATE)
        if duration_ms <= 0 or duration_ms > 30_001:
            raise TerminalProcessingError(
                "SOURCE_DURATION_INVALID",
                "音频时长无效",
            )

        try:
            output_probe = self.media_tools.probe(mp3_path)
        except MediaProbeFailed as error:
            raise TransientProcessingError(
                "AUDIO_OUTPUT_INVALID",
                "音频处理结果校验失败",
            ) from error
        output_stream = output_probe.first_audio_stream()
        if (
            output_stream is None
            or output_stream.codec_name != "mp3"
            or output_stream.sample_rate != SAMPLE_RATE
            or output_stream.channels != channels
        ):
            raise TransientProcessingError(
                "AUDIO_OUTPUT_INVALID",
                "音频处理结果校验失败",
            )
        if (
            output_stream.bit_rate is not None
            and not 120_000 <= output_stream.bit_rate <= 136_000
        ):
            raise TransientProcessingError(
                "AUDIO_OUTPUT_INVALID",
                "音频码率校验失败",
            )

        return NormalizedAudio(
            mp3_key=mp3_key,
            pcm_key=pcm_key,
            metadata_key=metadata_key,
            duration_ms=duration_ms,
            channels=channels,
            sample_rate=SAMPLE_RATE,
            mp3_byte_length=mp3_path.stat().st_size,
            pcm_byte_length=pcm_byte_length,
            mp3_sha256=_sha256_file(mp3_path),
            pcm_sha256=_sha256_file(pcm_path),
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_like_tool_configuration_error(diagnostic: str) -> bool:
    lowered = diagnostic.lower()
    return any(
        marker in lowered
        for marker in (
            "unknown encoder",
            "no such filter",
            "error initializing output stream",
        )
    )
