from __future__ import annotations

import struct
from pathlib import Path


class InvalidMedia(ValueError):
    pass


AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".flac", ".aac", ".m4a"}


def validate_audio(path: Path, original_name: str) -> str:
    suffix = Path(original_name).suffix.lower()
    if suffix not in AUDIO_EXTENSIONS:
        raise InvalidMedia(f"不支持的音频格式：{suffix or '无扩展名'}")
    header = _read_header(path, 16)
    valid = {
        ".wav": header.startswith(b"RIFF") and header[8:12] == b"WAVE",
        ".mp3": header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0),
        ".ogg": header.startswith(b"OggS"),
        ".flac": header.startswith(b"fLaC"),
        ".aac": len(header) >= 2 and header[0] == 0xFF and header[1] & 0xF0 == 0xF0,
        ".m4a": len(header) >= 12 and header[4:8] == b"ftyp",
    }[suffix]
    if not valid:
        raise InvalidMedia("音频文件头与扩展名不匹配")
    return suffix


def validate_stl(path: Path) -> None:
    size = path.stat().st_size
    if size < 15:
        raise InvalidMedia("STL 文件过小")
    header = _read_header(path, 84)
    if len(header) >= 84:
        triangle_count = struct.unpack("<I", header[80:84])[0]
        if 84 + triangle_count * 50 == size:
            return
    sample = _read_header(path, min(size, 4096)).lstrip().lower()
    if not (sample.startswith(b"solid") and b"facet" in sample):
        raise InvalidMedia("不是有效的 ASCII 或二进制 STL 文件")


def validate_webm(path: Path) -> None:
    if _read_header(path, 4) != b"\x1aE\xdf\xa3":
        raise InvalidMedia("WebM 文件缺少有效的 EBML 文件头")


def _read_header(path: Path, length: int) -> bytes:
    with path.open("rb") as file:
        return file.read(length)

