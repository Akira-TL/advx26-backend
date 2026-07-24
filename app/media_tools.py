from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


class MediaToolError(RuntimeError):
    def __init__(self, message: str, *, diagnostic: str = "") -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class MediaToolUnavailable(MediaToolError):
    pass


class MediaToolTimeout(MediaToolError):
    pass


class MediaCommandFailed(MediaToolError):
    pass


class MediaProbeFailed(MediaToolError):
    pass


@dataclass(frozen=True, slots=True)
class ProbeStream:
    index: int
    codec_type: str
    codec_name: str | None
    codec_tag: str | None
    channels: int | None
    sample_rate: int | None
    bit_rate: int | None
    encrypted: bool


@dataclass(frozen=True, slots=True)
class MediaProbe:
    format_name: str | None
    duration_seconds: float | None
    streams: tuple[ProbeStream, ...]

    def first_audio_stream(self) -> ProbeStream | None:
        return next(
            (
                stream
                for stream in self.streams
                if stream.index >= 0
                and stream.codec_type == "audio"
                and stream.codec_name
                and not stream.encrypted
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    def run(self, args: Sequence[str], *, timeout_seconds: float) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(self, args: Sequence[str], *, timeout_seconds: float) -> CommandResult:
        try:
            completed = subprocess.run(
                list(args),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as error:
            raise MediaToolUnavailable("媒体处理工具不可用") from error
        except subprocess.TimeoutExpired as error:
            stderr = _bounded_text(error.stderr)
            raise MediaToolTimeout(
                "媒体处理工具执行超时",
                diagnostic=stderr,
            ) from error
        if completed.returncode != 0:
            raise MediaCommandFailed(
                "媒体处理命令失败",
                diagnostic=_bounded_text(completed.stderr),
            )
        return CommandResult(stdout=completed.stdout, stderr=completed.stderr)


class MediaTools:
    def __init__(
        self,
        *,
        ffmpeg_binary: str = "ffmpeg",
        ffprobe_binary: str = "ffprobe",
        timeout_seconds: float = 90,
        runner: CommandRunner | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.ffmpeg_binary = ffmpeg_binary
        self.ffprobe_binary = ffprobe_binary
        self.timeout_seconds = timeout_seconds
        self.runner = runner or SubprocessCommandRunner()

    def check(self) -> None:
        self.runner.run(
            [self.ffmpeg_binary, "-version"],
            timeout_seconds=min(self.timeout_seconds, 10),
        )
        self.runner.run(
            [self.ffprobe_binary, "-version"],
            timeout_seconds=min(self.timeout_seconds, 10),
        )

    def probe(self, path: Path) -> MediaProbe:
        try:
            result = self.runner.run(
                [
                    self.ffprobe_binary,
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_streams",
                    "-show_format",
                    str(path),
                ],
                timeout_seconds=self.timeout_seconds,
            )
        except MediaCommandFailed as error:
            raise MediaProbeFailed(
                "无法解析媒体文件",
                diagnostic=error.diagnostic,
            ) from error
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise MediaProbeFailed("FFprobe 返回了无效结果") from error

        if not isinstance(payload, dict):
            raise MediaProbeFailed("FFprobe 返回了无效结果")
        raw_streams = payload.get("streams") or []
        if not isinstance(raw_streams, list):
            raise MediaProbeFailed("FFprobe 返回了无效轨道列表")
        streams = tuple(_parse_stream(item) for item in raw_streams)
        format_info = payload.get("format") or {}
        if not isinstance(format_info, dict):
            format_info = {}
        return MediaProbe(
            format_name=_optional_text(format_info.get("format_name")),
            duration_seconds=_optional_float(format_info.get("duration")),
            streams=streams,
        )

    def encode_h264_png_sequence(
        self,
        *,
        frame_pattern: Path,
        frame_count: int,
        output_path: Path,
    ) -> None:
        if frame_count <= 0:
            raise ValueError("frame_count must be positive")
        self.runner.run(
            [
                self.ffmpeg_binary,
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-fflags",
                "+bitexact",
                "-framerate",
                "10",
                "-start_number",
                "0",
                "-i",
                str(frame_pattern),
                "-frames:v",
                str(frame_count),
                "-map",
                "0:v:0",
                "-map_metadata",
                "-1",
                "-an",
                "-sn",
                "-dn",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "25.6",
                "-profile:v",
                "baseline",
                "-level:v",
                "3.0",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "10",
                "-g",
                "10",
                "-keyint_min",
                "10",
                "-sc_threshold",
                "0",
                "-bf",
                "0",
                "-threads",
                "1",
                "-x264-params",
                "open-gop=0:force-cfr=1:repeat-headers=1",
                "-movflags",
                "+faststart",
                "-video_track_timescale",
                "1000",
                "-use_editlist",
                "0",
                str(output_path),
            ],
            timeout_seconds=self.timeout_seconds,
        )

    def inspect_video(self, path: Path) -> dict[str, object]:
        result = self.runner.run(
            [
                self.ffprobe_binary,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_packets",
                "-show_frames",
                str(path),
            ],
            timeout_seconds=self.timeout_seconds,
        )
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MediaProbeFailed("FFprobe 返回了无效视频结果") from error
        if not isinstance(payload, dict):
            raise MediaProbeFailed("FFprobe 返回了无效视频结果")
        combined = payload.pop("packets_and_frames", None)
        if isinstance(combined, list):
            payload["packets"] = [
                item for item in combined
                if isinstance(item, dict) and item.get("type") == "packet"
            ]
            payload["frames"] = [
                item for item in combined
                if isinstance(item, dict) and item.get("type") == "frame"
            ]
        return payload

    def normalize_audio(
        self,
        *,
        source_path: Path,
        stream_index: int,
        channels: int,
        mp3_path: Path,
        pcm_path: Path,
    ) -> None:
        if channels not in {1, 2}:
            raise ValueError("channels must be 1 or 2")
        layout = "mono" if channels == 1 else "stereo"
        filter_graph = (
            f"[0:{stream_index}]"
            "atrim=start=0:end=30,"
            "asetpts=PTS-STARTPTS,"
            "aresample=44100,"
            f"aformat=channel_layouts={layout},"
            "alimiter=limit=0.891251:level=false,"
            "asplit=outputs=2[mp3][pcm]"
        )
        self.runner.run(
            [
                self.ffmpeg_binary,
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-fflags",
                "+discardcorrupt",
                "-i",
                str(source_path),
                "-filter_complex",
                filter_graph,
                "-map",
                "[mp3]",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "128k",
                "-ar",
                "44100",
                "-ac",
                str(channels),
                "-write_xing",
                "0",
                str(mp3_path),
                "-map",
                "[pcm]",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-c:a",
                "pcm_s16le",
                "-ar",
                "44100",
                "-ac",
                str(channels),
                "-f",
                "s16le",
                str(pcm_path),
            ],
            timeout_seconds=self.timeout_seconds,
        )


def _parse_stream(item: object) -> ProbeStream:
    data = item if isinstance(item, dict) else {}
    side_data = data.get("side_data_list") or []
    codec_tag = _optional_text(data.get("codec_tag_string"))
    index = _optional_int(data.get("index"))
    encrypted = (codec_tag or "").lower() in {"enca", "encv"}
    encrypted = encrypted or any(
        "encrypt" in str(entry.get("side_data_type", "")).lower()
        for entry in side_data
        if isinstance(entry, dict)
    )
    return ProbeStream(
        index=index if index is not None else -1,
        codec_type=str(data.get("codec_type", "")),
        codec_name=_optional_text(data.get("codec_name")),
        codec_tag=codec_tag,
        channels=_optional_int(data.get("channels")),
        sample_rate=_optional_int(data.get("sample_rate")),
        bit_rate=_optional_int(data.get("bit_rate")),
        encrypted=encrypted,
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _bounded_text(value: bytes | str | None, limit: int = 8192) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    return text[-limit:]
