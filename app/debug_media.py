from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response


RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")


def create_debug_media_router(*, media_dir: Path) -> APIRouter:
    router = APIRouter(tags=["debug"])
    root = Path(media_dir).resolve()

    @router.head(
        "/debug/audio.mp3",
        response_class=Response,
        operation_id="headDebugAudio",
        include_in_schema=False,
    )
    @router.get(
        "/debug/audio.mp3",
        response_class=Response,
        operation_id="getDebugAudio",
        include_in_schema=False,
    )
    async def debug_audio(request: Request) -> Response:
        return _serve_file(
            request=request,
            path=root / "monitoring-30s.mp3",
            media_type="audio/mpeg",
        )

    @router.head(
        "/debug/video.mp4",
        response_class=Response,
        operation_id="headDebugVideo",
        include_in_schema=False,
    )
    @router.get(
        "/debug/video.mp4",
        response_class=Response,
        operation_id="getDebugVideo",
        include_in_schema=False,
    )
    async def debug_video(request: Request) -> Response:
        return _serve_file(
            request=request,
            path=root / "monitoring-30s.mp4",
            media_type="video/mp4",
        )

    return router


def _serve_file(*, request: Request, path: Path, media_type: str) -> Response:
    try:
        payload = path.read_bytes()
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="调试媒体尚未生成") from error
    except OSError as error:
        raise HTTPException(status_code=503, detail="调试媒体不可用") from error

    total = len(payload)
    digest = hashlib.sha256(payload).hexdigest()
    etag = f'"{digest}"'
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "ETag": etag,
    }
    start, end, status_code = 0, total - 1, 200
    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if range_header and (if_range is None or if_range == etag):
        parsed = _parse_range(range_header, total)
        if parsed is None:
            return Response(
                status_code=416,
                headers={
                    **headers,
                    "Content-Range": f"bytes */{total}",
                    "Content-Length": "0",
                },
            )
        start, end = parsed
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"

    body = payload[start : end + 1]
    headers["Content-Length"] = str(len(body))
    if request.method == "HEAD":
        return Response(status_code=status_code, headers=headers, media_type=media_type)
    return Response(
        content=body,
        status_code=status_code,
        headers=headers,
        media_type=media_type,
    )


def _parse_range(value: str, size: int) -> tuple[int, int] | None:
    if "," in value or size <= 0:
        return None
    match = RANGE_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:
        suffix = int(last)
        if suffix <= 0:
            return None
        return max(0, size - suffix), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)
