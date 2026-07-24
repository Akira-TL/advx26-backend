from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO


class InvalidObjectKey(ValueError):
    """Raised when an object key is empty, absolute, or can escape its root."""


@dataclass(frozen=True, slots=True)
class ObjectStat:
    key: str
    byte_length: int
    modified_ns: int


class FileSystemObjectStore:
    """Filesystem-backed object storage addressed only through opaque POSIX keys."""

    def __init__(
        self,
        *,
        objects_root: Path,
        staging_root: Path,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.objects_root = Path(objects_root).resolve()
        self.staging_root = Path(staging_root).resolve()
        self.chunk_size = chunk_size

    def initialize(self) -> None:
        self._ensure_root(self.objects_root)
        self._ensure_root(self.staging_root)
        self._ensure_atomic_promotion_supported()

    def put(self, key: str, source: BinaryIO) -> ObjectStat:
        return self._put(self.objects_root, key, source)

    def put_staging(self, key: str, source: BinaryIO) -> ObjectStat:
        return self._put(self.staging_root, key, source)

    def replace_staging(self, key: str, source: BinaryIO) -> ObjectStat:
        return self._put(self.staging_root, key, source, replace=True)

    def open(self, key: str) -> BinaryIO:
        return self._path(self.objects_root, key).open("rb")

    def open_staging(self, key: str) -> BinaryIO:
        return self._path(self.staging_root, key).open("rb")

    def stat(self, key: str) -> ObjectStat:
        return self._stat(self.objects_root, key)

    def stat_staging(self, key: str) -> ObjectStat:
        return self._stat(self.staging_root, key)

    def staging_prefix_size(self, prefix: str) -> int:
        path = self._path(self.staging_root, prefix)
        if not path.exists():
            return 0
        if path.is_file():
            return path.stat().st_size
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())

    def delete_staging_prefix(self, prefix: str) -> bool:
        path = self._path(self.staging_root, prefix)
        if not path.exists():
            return False
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        self._remove_empty_parents(path.parent, self.staging_root)
        return True

    def iter_range(
        self,
        key: str,
        *,
        start: int,
        length: int,
        chunk_size: int | None = None,
    ) -> Iterator[bytes]:
        if start < 0:
            raise ValueError("start must be non-negative")
        if length < 0:
            raise ValueError("length must be non-negative")
        read_size = chunk_size or self.chunk_size
        if read_size <= 0:
            raise ValueError("chunk_size must be positive")

        with self.open(key) as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(read_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    def delete(self, key: str) -> bool:
        path = self._path(self.objects_root, key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        self._remove_empty_parents(path.parent, self.objects_root)
        return True

    def promote_staging(self, staging_prefix: str, destination_prefix: str) -> None:
        source = self._path(self.staging_root, staging_prefix)
        destination = self._path(self.objects_root, destination_prefix)
        if not source.exists():
            raise FileNotFoundError(staging_prefix)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(destination_prefix)
        os.rename(source, destination)
        self._remove_empty_parents(source.parent, self.staging_root)

    def check(self) -> None:
        self._check_root(self.objects_root)
        self._check_root(self.staging_root)
        self._ensure_atomic_promotion_supported()

    def _put(
        self,
        root: Path,
        key: str,
        source: BinaryIO,
        *,
        replace: bool = False,
    ) -> ObjectStat:
        destination = self._path(root, key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix=".object-write-",
                dir=destination.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                while chunk := source.read(self.chunk_size):
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())
            if replace:
                os.replace(temporary_path, destination)
                temporary_path = None
            else:
                os.link(temporary_path, destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return self._stat(root, key)

    def _stat(self, root: Path, key: str) -> ObjectStat:
        path = self._path(root, key)
        metadata = path.stat()
        if not path.is_file():
            raise FileNotFoundError(key)
        return ObjectStat(
            key=self._normalize_key(key),
            byte_length=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
        )

    def _path(self, root: Path, key: str) -> Path:
        normalized = self._normalize_key(key)
        candidate = (root / PurePosixPath(normalized)).resolve()
        if candidate == root or root not in candidate.parents:
            raise InvalidObjectKey(f"object key escapes storage root: {key!r}")
        return candidate

    @staticmethod
    def _normalize_key(key: str) -> str:
        if not isinstance(key, str) or not key or "\\" in key or "\x00" in key:
            raise InvalidObjectKey(f"invalid object key: {key!r}")
        path = PurePosixPath(key)
        if path.is_absolute():
            raise InvalidObjectKey(f"absolute object key is not allowed: {key!r}")
        parts = key.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise InvalidObjectKey(f"invalid object key segment: {key!r}")
        return "/".join(parts)

    @staticmethod
    def _ensure_root(root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise NotADirectoryError(root)

    @classmethod
    def _check_root(cls, root: Path) -> None:
        cls._ensure_root(root)
        probe = root / f".write-probe-{uuid.uuid4().hex}"
        try:
            with probe.open("xb") as output:
                output.write(b"ok")
                output.flush()
                os.fsync(output.fileno())
        finally:
            probe.unlink(missing_ok=True)

    def _ensure_atomic_promotion_supported(self) -> None:
        if self.objects_root.stat().st_dev != self.staging_root.stat().st_dev:
            raise OSError("object and staging roots must share one filesystem")

    @staticmethod
    def _remove_empty_parents(path: Path, root: Path) -> None:
        while path != root and root in path.parents:
            try:
                path.rmdir()
            except OSError:
                break
            path = path.parent
