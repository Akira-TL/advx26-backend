from __future__ import annotations

import hashlib
import io
import json
import math
import struct
import zlib
from dataclasses import dataclass

from .job_repository import ClaimedJob
from .object_store import FileSystemObjectStore
from .processing_worker import TerminalProcessingError, TransientProcessingError


INDEX_MAGIC = b"AIX1"
INDEX_VERSION = 1
INDEX_HEADER = struct.Struct("<4sHHII")
INDEX_RECORD = struct.Struct("<IIII")


class InvalidMp3(ValueError):
    pass


class InvalidAudioIndex(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Mp3Frame:
    sample_position: int
    byte_offset: int
    byte_length: int
    samples_per_frame: int
    sample_rate: int
    bit_rate: int
    channels: int
    crc32: int


@dataclass(frozen=True, slots=True)
class IndexedMp3:
    audio_key: str
    index_key: str
    metadata_key: str
    audio_byte_length: int
    audio_sha256: str
    audio_etag: str
    index_byte_length: int
    index_sha256: str
    index_etag: str
    sample_rate: int
    bit_rate: int
    channels: int
    index_version: int
    frame_count: int
    duration_ms: int
    indexed_duration_ms: int


class Mp3Indexer:
    def __init__(self, object_store: FileSystemObjectStore) -> None:
        self.object_store = object_store

    def build(self, job: ClaimedJob, *, authoritative_duration_ms: int) -> IndexedMp3:
        audio_key = f"jobs/{job.job_id}/normalized/audio.mp3"
        index_key = f"jobs/{job.job_id}/indexed/audio.idx"
        metadata_key = f"jobs/{job.job_id}/indexed/audio-index.json"
        try:
            with self.object_store.open_staging(audio_key) as source:
                audio = source.read()
            frames = parse_mp3_frames(audio)
            index = build_audio_index(frames)
            validate_audio_index(
                audio,
                index,
                authoritative_duration_ms=authoritative_duration_ms,
            )
        except FileNotFoundError as error:
            raise TransientProcessingError(
                "NORMALIZED_AUDIO_MISSING",
                "归一化音频暂不可用",
            ) from error
        except (InvalidMp3, InvalidAudioIndex) as error:
            raise TerminalProcessingError(
                "NORMALIZED_MP3_INVALID",
                "归一化 MP3 或索引无效",
            ) from error

        audio_sha256 = hashlib.sha256(audio).hexdigest()
        index_sha256 = hashlib.sha256(index).hexdigest()
        indexed_duration_ms = round(
            (frames[-1].sample_position + frames[-1].samples_per_frame)
            * 1000
            / frames[0].sample_rate
        )
        result = IndexedMp3(
            audio_key=audio_key,
            index_key=index_key,
            metadata_key=metadata_key,
            audio_byte_length=len(audio),
            audio_sha256=audio_sha256,
            audio_etag=f'"{audio_sha256}"',
            index_byte_length=len(index),
            index_sha256=index_sha256,
            index_etag=f'"{index_sha256}"',
            sample_rate=frames[0].sample_rate,
            bit_rate=frames[0].bit_rate,
            channels=frames[0].channels,
            index_version=INDEX_VERSION,
            frame_count=len(frames),
            duration_ms=authoritative_duration_ms,
            indexed_duration_ms=indexed_duration_ms,
        )
        metadata = json.dumps(
            {
                "schema_version": 1,
                "content_id": job.content_id,
                "audio": {
                    "object_key": result.audio_key,
                    "byte_length": result.audio_byte_length,
                    "sha256": result.audio_sha256,
                    "etag": result.audio_etag,
                    "sample_rate": result.sample_rate,
                    "bit_rate": result.bit_rate,
                    "channels": result.channels,
                },
                "index": {
                    "object_key": result.index_key,
                    "byte_length": result.index_byte_length,
                    "sha256": result.index_sha256,
                    "etag": result.index_etag,
                    "version": result.index_version,
                    "record_count": result.frame_count,
                },
                "duration_ms": result.duration_ms,
                "indexed_duration_ms": result.indexed_duration_ms,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.object_store.replace_staging(index_key, io.BytesIO(index))
        self.object_store.replace_staging(metadata_key, io.BytesIO(metadata))
        return result


def parse_mp3_frames(audio: bytes) -> tuple[Mp3Frame, ...]:
    if not audio:
        raise InvalidMp3("empty MP3")
    offset = _skip_id3v2(audio)
    end = _trim_id3v1(audio)
    frames: list[Mp3Frame] = []
    sample_position = 0
    expected: tuple[int, int, int] | None = None

    while offset < end:
        if end - offset < 4:
            raise InvalidMp3("truncated MP3 frame header")
        header = int.from_bytes(audio[offset : offset + 4], "big")
        parsed = _parse_frame_header(header)
        frame_length, samples_per_frame, sample_rate, bit_rate, channels = parsed
        frame_end = offset + frame_length
        if frame_end > end:
            raise InvalidMp3("truncated MP3 frame")
        profile = (sample_rate, bit_rate, channels)
        if expected is None:
            expected = profile
        elif expected != profile:
            raise InvalidMp3("MP3 frame profile changes within asset")
        frame_bytes = audio[offset:frame_end]
        frames.append(
            Mp3Frame(
                sample_position=sample_position,
                byte_offset=offset,
                byte_length=frame_length,
                samples_per_frame=samples_per_frame,
                sample_rate=sample_rate,
                bit_rate=bit_rate,
                channels=channels,
                crc32=zlib.crc32(frame_bytes) & 0xFFFFFFFF,
            )
        )
        sample_position += samples_per_frame
        offset = frame_end

    if not frames:
        raise InvalidMp3("MP3 contains no complete frames")
    return tuple(frames)


def build_audio_index(frames: tuple[Mp3Frame, ...]) -> bytes:
    if not frames:
        raise InvalidAudioIndex("cannot index empty frame list")
    output = bytearray(
        INDEX_HEADER.pack(
            INDEX_MAGIC,
            INDEX_VERSION,
            INDEX_RECORD.size,
            len(frames),
            0,
        )
    )
    for frame in frames:
        for value in (
            frame.sample_position,
            frame.byte_offset,
            frame.byte_length,
            frame.crc32,
        ):
            if not 0 <= value <= 0xFFFFFFFF:
                raise InvalidAudioIndex("index value exceeds uint32")
        output.extend(
            INDEX_RECORD.pack(
                frame.sample_position,
                frame.byte_offset,
                frame.byte_length,
                frame.crc32,
            )
        )
    return bytes(output)


def validate_audio_index(
    audio: bytes,
    index: bytes,
    *,
    authoritative_duration_ms: int,
) -> tuple[Mp3Frame, ...]:
    if len(index) < INDEX_HEADER.size:
        raise InvalidAudioIndex("truncated index header")
    magic, version, record_size, record_count, reserved = INDEX_HEADER.unpack_from(index)
    if magic != INDEX_MAGIC or version != INDEX_VERSION:
        raise InvalidAudioIndex("unsupported index format")
    if record_size != INDEX_RECORD.size or reserved != 0:
        raise InvalidAudioIndex("invalid index header")
    expected_size = INDEX_HEADER.size + record_count * record_size
    if len(index) != expected_size or record_count == 0:
        raise InvalidAudioIndex("invalid index length")

    parsed_frames = parse_mp3_frames(audio)
    if len(parsed_frames) != record_count:
        raise InvalidAudioIndex("frame count mismatch")
    previous_end = 0
    previous_sample = -1
    for position, frame in enumerate(parsed_frames):
        record = INDEX_RECORD.unpack_from(index, INDEX_HEADER.size + position * record_size)
        sample_position, byte_offset, byte_length, crc32 = record
        if sample_position <= previous_sample or byte_offset < previous_end:
            raise InvalidAudioIndex("non-monotonic or overlapping record")
        if byte_length == 0 or byte_offset + byte_length > len(audio):
            raise InvalidAudioIndex("record is out of bounds")
        if record != (
            frame.sample_position,
            frame.byte_offset,
            frame.byte_length,
            frame.crc32,
        ):
            raise InvalidAudioIndex("record does not match MP3 frame")
        if zlib.crc32(audio[byte_offset : byte_offset + byte_length]) & 0xFFFFFFFF != crc32:
            raise InvalidAudioIndex("frame CRC mismatch")
        previous_sample = sample_position
        previous_end = byte_offset + byte_length

    indexed_samples = (
        parsed_frames[-1].sample_position + parsed_frames[-1].samples_per_frame
    )
    indexed_duration_ms = round(
        indexed_samples * 1000 / parsed_frames[0].sample_rate
    )
    tolerance_ms = math.ceil(
        2 * parsed_frames[0].samples_per_frame * 1000 / parsed_frames[0].sample_rate
    )
    if abs(indexed_duration_ms - authoritative_duration_ms) > tolerance_ms:
        raise InvalidAudioIndex("indexed duration differs from authoritative duration")
    return parsed_frames


def _skip_id3v2(audio: bytes) -> int:
    if not audio.startswith(b"ID3"):
        return 0
    if len(audio) < 10:
        raise InvalidMp3("truncated ID3v2 header")
    size_bytes = audio[6:10]
    if any(value & 0x80 for value in size_bytes):
        raise InvalidMp3("invalid ID3v2 syncsafe size")
    size = (
        (size_bytes[0] << 21)
        | (size_bytes[1] << 14)
        | (size_bytes[2] << 7)
        | size_bytes[3]
    )
    footer = 10 if audio[5] & 0x10 else 0
    offset = 10 + size + footer
    if offset > len(audio):
        raise InvalidMp3("ID3v2 tag exceeds file")
    return offset


def _trim_id3v1(audio: bytes) -> int:
    if len(audio) >= 128 and audio[-128:-125] == b"TAG":
        return len(audio) - 128
    return len(audio)


def _parse_frame_header(header: int) -> tuple[int, int, int, int, int]:
    if header >> 21 != 0x7FF:
        raise InvalidMp3("invalid MP3 sync word")
    version_bits = (header >> 19) & 0b11
    layer_bits = (header >> 17) & 0b11
    bitrate_index = (header >> 12) & 0xF
    sample_rate_index = (header >> 10) & 0b11
    padding = (header >> 9) & 1
    channel_mode = (header >> 6) & 0b11
    if version_bits == 0b01 or layer_bits != 0b01:
        raise InvalidMp3("unsupported MPEG version or layer")
    if bitrate_index in {0, 15} or sample_rate_index == 3:
        raise InvalidMp3("unsupported free or invalid MP3 frame")

    version = {0b11: 1, 0b10: 2, 0b00: 25}[version_bits]
    sample_rate_base = (44_100, 48_000, 32_000)[sample_rate_index]
    sample_rate = sample_rate_base if version == 1 else sample_rate_base // (2 if version == 2 else 4)
    if version == 1:
        bitrate_kbps = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)[bitrate_index - 1]
        samples_per_frame = 1152
        coefficient = 144
    else:
        bitrate_kbps = (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)[bitrate_index - 1]
        samples_per_frame = 576
        coefficient = 72
    bit_rate = bitrate_kbps * 1000
    frame_length = coefficient * bit_rate // sample_rate + padding
    if frame_length < 4:
        raise InvalidMp3("invalid MP3 frame length")
    channels = 1 if channel_mode == 0b11 else 2
    return frame_length, samples_per_frame, sample_rate, bit_rate, channels
