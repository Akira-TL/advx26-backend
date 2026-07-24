from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Protocol

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


WIDTH = 480
HEIGHT = 320
FRAME_RATE = 10


class StageSink(Protocol):
    async def stage(self, name: str) -> None: ...


class InvalidVideoOutput(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EncodedVideo:
    object_key: str
    metadata_key: str
    byte_length: int
    sha256: str
    etag: str
    duration_ms: int
    frame_count: int
    width: int
    height: int
    frame_rate: int
    codec: str
    profile: str
    pixel_format: str


class H264VideoEncoder:
    def __init__(
        self,
        *,
        object_store: FileSystemObjectStore,
        media_tools: MediaTools,
    ) -> None:
        self.object_store = object_store
        self.media_tools = media_tools

    async def encode(
        self,
        job: ClaimedJob,
        reporter: StageSink,
        *,
        frames_dir: Path,
        authoritative_duration_ms: int,
    ) -> EncodedVideo:
        await reporter.stage("ENCODING_VIDEO")
        try:
            return await asyncio.to_thread(
                self._encode_sync,
                job,
                Path(frames_dir),
                authoritative_duration_ms,
            )
        except (MediaToolUnavailable, MediaToolTimeout) as error:
            raise TransientProcessingError(
                "VIDEO_TOOL_TEMPORARY",
                "视频编码工具暂不可用",
            ) from error
        except MediaCommandFailed as error:
            raise TerminalProcessingError(
                "VIDEO_ENCODE_FAILED",
                "可视化帧编码失败",
            ) from error
        except (InvalidVideoOutput, MediaProbeFailed) as error:
            raise TerminalProcessingError(
                "VIDEO_OUTPUT_INVALID",
                "生成的视频不符合播放规格",
            ) from error
        except OSError as error:
            raise TransientProcessingError(
                "VIDEO_STORAGE_TEMPORARY",
                "视频暂存失败",
            ) from error

    def _encode_sync(
        self,
        job: ClaimedJob,
        frames_dir: Path,
        authoritative_duration_ms: int,
    ) -> EncodedVideo:
        if authoritative_duration_ms <= 0:
            raise InvalidVideoOutput("authoritative duration must be positive")
        frame_count = math.ceil(authoritative_duration_ms * FRAME_RATE / 1000)
        _validate_frame_sequence(frames_dir, frame_count)
        with tempfile.TemporaryDirectory(prefix="cloud-video-") as directory:
            output_path = Path(directory) / "video.mp4"
            self.media_tools.encode_h264_png_sequence(
                frame_pattern=frames_dir / "frame-%06d.png",
                frame_count=frame_count,
                output_path=output_path,
            )
            payload = output_path.read_bytes()
            probe = self.media_tools.inspect_video(output_path)
            video_duration_ms = validate_h264_mp4(
                payload,
                probe,
                frame_count=frame_count,
                authoritative_duration_ms=authoritative_duration_ms,
            )

        sha256 = hashlib.sha256(payload).hexdigest()
        object_key = f"jobs/{job.job_id}/video/video.mp4"
        metadata_key = f"jobs/{job.job_id}/video/video.json"
        result = EncodedVideo(
            object_key=object_key,
            metadata_key=metadata_key,
            byte_length=len(payload),
            sha256=sha256,
            etag=f'"{sha256}"',
            duration_ms=video_duration_ms,
            frame_count=frame_count,
            width=WIDTH,
            height=HEIGHT,
            frame_rate=FRAME_RATE,
            codec="h264",
            profile="Constrained Baseline",
            pixel_format="yuv420p",
        )
        metadata = json.dumps(
            {
                "schema_version": 1,
                "content_id": job.content_id,
                "object_key": result.object_key,
                "byte_length": result.byte_length,
                "sha256": result.sha256,
                "etag": result.etag,
                "duration_ms": result.duration_ms,
                "frame_count": result.frame_count,
                "width": result.width,
                "height": result.height,
                "frame_rate": result.frame_rate,
                "codec": result.codec,
                "profile": result.profile,
                "pixel_format": result.pixel_format,
                "max_keyframe_interval_frames": FRAME_RATE,
                "has_b_frames": False,
                "fast_start": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.object_store.replace_staging(object_key, io.BytesIO(payload))
        self.object_store.replace_staging(metadata_key, io.BytesIO(metadata))
        return result


def validate_h264_mp4(
    payload: bytes,
    probe: dict[str, object],
    *,
    frame_count: int,
    authoritative_duration_ms: int,
) -> int:
    boxes = _top_level_boxes(payload)
    box_types = [box_type for box_type, _, _ in boxes]
    if b"moof" in box_types or b"moov" not in box_types or b"mdat" not in box_types:
        raise InvalidVideoOutput("MP4 is fragmented or incomplete")
    if box_types.index(b"moov") > box_types.index(b"mdat"):
        raise InvalidVideoOutput("MP4 is not fast-start")
    moov_type, moov_offset, moov_size = next(box for box in boxes if box[0] == b"moov")
    del moov_type
    moov = payload[moov_offset : moov_offset + moov_size]
    if b"avcC" not in moov or b"stbl" not in moov or b"stsz" not in moov:
        raise InvalidVideoOutput("required AVC sample tables are missing")
    if b"edts" in moov:
        raise InvalidVideoOutput("edit lists are not supported")

    streams = probe.get("streams")
    packets = probe.get("packets")
    frames = probe.get("frames")
    if not isinstance(streams, list) or len(streams) != 1:
        raise InvalidVideoOutput("video must contain exactly one stream")
    if not isinstance(packets, list) or not isinstance(frames, list):
        raise InvalidVideoOutput("FFprobe packet or frame data is missing")
    stream = streams[0]
    if not isinstance(stream, dict) or stream.get("codec_type") != "video":
        raise InvalidVideoOutput("only one video stream is allowed")
    expected = {
        "codec_name": "h264",
        "profile": "Constrained Baseline",
        "pix_fmt": "yuv420p",
        "width": WIDTH,
        "height": HEIGHT,
        "has_b_frames": 0,
    }
    for key, value in expected.items():
        if stream.get(key) != value:
            raise InvalidVideoOutput(f"unexpected video field {key}")
    if stream.get("field_order") not in {None, "progressive"}:
        raise InvalidVideoOutput("interlaced video is not supported")
    if stream.get("codec_tag_string") not in {None, "avc1"}:
        raise InvalidVideoOutput("unexpected AVC sample entry")
    if _fraction(stream.get("r_frame_rate")) != Fraction(FRAME_RATE, 1):
        raise InvalidVideoOutput("nominal frame rate is not 10 fps")
    if _fraction(stream.get("avg_frame_rate")) != Fraction(FRAME_RATE, 1):
        raise InvalidVideoOutput("average frame rate is not 10 fps")
    if int(stream.get("nb_frames", -1)) != frame_count:
        raise InvalidVideoOutput("stream frame count mismatch")

    expected_video_duration_ms = frame_count * 1000 // FRAME_RATE
    duration_ms = round(float(stream.get("duration", 0)) * 1000)
    if abs(duration_ms - expected_video_duration_ms) > 1:
        raise InvalidVideoOutput("video duration does not match frame count")
    if duration_ms < authoritative_duration_ms:
        raise InvalidVideoOutput("video does not cover authoritative audio duration")
    if duration_ms - authoritative_duration_ms >= 1000 / FRAME_RATE:
        raise InvalidVideoOutput("video tail exceeds one frame")

    video_packets = [item for item in packets if isinstance(item, dict) and item.get("codec_type") == "video"]
    video_frames = [item for item in frames if isinstance(item, dict) and item.get("media_type") == "video"]
    if len(video_packets) != frame_count or len(video_frames) != frame_count:
        raise InvalidVideoOutput("packet or decoded frame count mismatch")
    previous_dts = -math.inf
    previous_pts = -math.inf
    keyframe_indices: list[int] = []
    for index, packet in enumerate(video_packets):
        dts = float(packet.get("dts_time", "nan"))
        pts = float(packet.get("pts_time", "nan"))
        position = int(packet.get("pos", -1))
        size = int(packet.get("size", -1))
        if not math.isfinite(dts) or not math.isfinite(pts):
            raise InvalidVideoOutput("packet timestamps are missing")
        if dts <= previous_dts or pts <= previous_pts or abs(pts - dts) > 1e-6:
            raise InvalidVideoOutput("timestamps are not monotonic CFR without reordering")
        if position < 0 or size <= 0 or position + size > len(payload):
            raise InvalidVideoOutput("packet byte range exceeds MP4")
        if "K" in str(packet.get("flags", "")):
            keyframe_indices.append(index)
        previous_dts = dts
        previous_pts = pts
    if not keyframe_indices or keyframe_indices[0] != 0:
        raise InvalidVideoOutput("first video frame is not a keyframe")
    if any(b - a > FRAME_RATE for a, b in zip(keyframe_indices, keyframe_indices[1:])):
        raise InvalidVideoOutput("keyframe interval exceeds one second")
    if frame_count - keyframe_indices[-1] > FRAME_RATE:
        raise InvalidVideoOutput("final GOP exceeds one second")
    for frame in video_frames:
        if frame.get("pict_type") == "B" or int(frame.get("interlaced_frame", 0)) != 0:
            raise InvalidVideoOutput("B frames or interlacing are not supported")
    return duration_ms


def _validate_frame_sequence(frames_dir: Path, frame_count: int) -> None:
    if not frames_dir.is_dir():
        raise InvalidVideoOutput("frame directory is missing")
    expected = {f"frame-{index:06d}.png" for index in range(frame_count)}
    actual = {path.name for path in frames_dir.glob("frame-*.png") if path.is_file()}
    if actual != expected:
        raise InvalidVideoOutput("frame sequence is incomplete or contains extras")
    for filename in expected:
        path = frames_dir / filename
        if path.stat().st_size < 8 or path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            raise InvalidVideoOutput("frame is not a valid PNG")


def _top_level_boxes(payload: bytes) -> list[tuple[bytes, int, int]]:
    boxes: list[tuple[bytes, int, int]] = []
    offset = 0
    while offset < len(payload):
        if len(payload) - offset < 8:
            raise InvalidVideoOutput("truncated MP4 box")
        size = int.from_bytes(payload[offset : offset + 4], "big")
        box_type = payload[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if len(payload) - offset < 16:
                raise InvalidVideoOutput("truncated extended MP4 box")
            size = int.from_bytes(payload[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = len(payload) - offset
        if size < header_size or offset + size > len(payload):
            raise InvalidVideoOutput("invalid MP4 box size")
        boxes.append((box_type, offset, size))
        offset += size
    return boxes


def _fraction(value: object) -> Fraction:
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return Fraction(0, 1)
