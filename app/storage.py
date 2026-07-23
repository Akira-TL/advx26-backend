from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from collections.abc import Iterator
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import BinaryIO

from fastapi import UploadFile


class FileTooLarge(ValueError):
    pass


async def save_upload(
    upload: UploadFile,
    destination: Path,
    *,
    max_bytes: int,
    chunk_size: int,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with destination.open("xb") as output:
        while chunk := await upload.read(chunk_size):
            size += len(chunk)
            if size > max_bytes:
                raise FileTooLarge(f"文件超过限制：{max_bytes} bytes")
            digest.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    await upload.close()
    if size == 0:
        raise ValueError("不允许上传空文件")
    return size, digest.hexdigest()


def write_manifest(path: Path, manifest: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def iter_file(path: Path, start: int, length: int, chunk_size: int) -> Iterator[bytes]:
    with path.open("rb") as file:
        file.seek(start)
        remaining = length
        while remaining:
            chunk = file.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def build_bundle(package_dir: Path, filenames: list[str]) -> BinaryIO:
    output = SpooledTemporaryFile(max_size=32 * 1024 * 1024, mode="w+b")
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename in filenames:
            archive.write(package_dir / filename, arcname=filename)
    output.seek(0)
    return output


def remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)

