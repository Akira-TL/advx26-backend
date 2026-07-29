from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .auth import (
    DEVICE_ROLE_PLAYBACK,
    DEVICE_ROLE_TRIGGER,
    DevicePrincipal,
    DeviceTokenService,
)
from .database import Database
from .object_store import FileSystemObjectStore
from .openapi_config import error_responses, playback_asset_responses
from .schemas import CompactContent


CONTENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")
IMMUTABLE_CACHE = "private, max-age=31536000, immutable"
ASSET_KINDS = {
    "video": "VIDEO",
    "audio": "AUDIO",
    "audio-index": "AUDIO_INDEX",
    "replay-params": "REPLAY_PARAMS",
}


def create_device_router(
    *,
    database: Database,
    object_store: FileSystemObjectStore,
    tokens: DeviceTokenService,
) -> APIRouter:
    router = APIRouter(tags=["devices"])
    trigger_scheme = HTTPBearer(
        auto_error=False,
        scheme_name="TriggerToken",
        description="Fixed Bearer Token provisioned to the Trigger board.",
    )
    playback_scheme = HTTPBearer(
        auto_error=False,
        scheme_name="PlaybackToken",
        description="Fixed Bearer Token provisioned to the Playback board.",
    )

    def require_role(expected_role: str, scheme: HTTPBearer):
        async def dependency(
            credentials: HTTPAuthorizationCredentials | None = Security(scheme),
        ) -> DevicePrincipal:
            authorization = (
                f"{credentials.scheme} {credentials.credentials}"
                if credentials is not None
                else None
            )
            principal = tokens.authenticate(authorization)
            if principal is None:
                raise HTTPException(
                    status_code=401,
                    detail="无效或缺少设备 Token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if principal.role != expected_role:
                raise HTTPException(status_code=403, detail="设备角色无权访问此接口")
            return principal

        return dependency

    require_trigger = require_role(DEVICE_ROLE_TRIGGER, trigger_scheme)
    require_playback = require_role(DEVICE_ROLE_PLAYBACK, playback_scheme)

    @router.get(
        "/c/{content_id}",
        responses=error_responses(401, 403, 404, 503),
        summary="Resolve NFC content (device JSON or browser preview)",
        description="With Trigger Token: returns Compact Content JSON. Without auth: serves an HTML preview page for browsers.",
        operation_id="resolveTriggerContent",
    )
    async def resolve_compact_content(
        content_id: str,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(trigger_scheme),
    ) -> Response:
        _validate_content_id(content_id)

        if credentials is None:
            from .preview_page import serve_preview_page
            return serve_preview_page(database, content_id, request)

        authorization = f"{credentials.scheme} {credentials.credentials}"
        principal = tokens.authenticate(authorization)
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail="无效或缺少设备 Token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if principal.role != DEVICE_ROLE_TRIGGER:
            raise HTTPException(status_code=403, detail="设备角色无权访问此接口")

        row = database.get_ready_media_object(content_id=content_id, kind="MANIFEST")
        if row is None:
            raise HTTPException(status_code=404, detail="内容不存在或尚未就绪")
        payload = _read_verified_object(object_store, row)
        try:
            manifest = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=503, detail="内容清单损坏") from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("content_id") != content_id
            or manifest.get("state") != "READY"
        ):
            raise HTTPException(status_code=503, detail="内容清单损坏")
        resolved = copy.deepcopy(manifest)
        _absolutize_manifest_urls(resolved, str(request.base_url).rstrip("/"))
        body = json.dumps(
            resolved,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Content-Length": str(len(body)),
                "ETag": f'"{digest}"',
                "Cache-Control": IMMUTABLE_CACHE,
                "Vary": "Authorization",
            },
        )

    @router.head(
        "/api/v1/contents/{content_id}/assets/{asset_kind}",
        response_class=Response,
        responses=playback_asset_responses(include_body=False),
        summary="Inspect immutable playback asset",
        description="Playback-only HEAD response with Range and If-Range support.",
        operation_id="headPlaybackAsset",
    )
    @router.get(
        "/api/v1/contents/{content_id}/assets/{asset_kind}",
        response_class=Response,
        responses=playback_asset_responses(),
        summary="Download immutable playback asset",
        description="Playback-only GET response with Range and If-Range support.",
        operation_id="getPlaybackAsset",
    )
    async def get_playback_asset(
        content_id: str,
        asset_kind: str,
        request: Request,
        _: DevicePrincipal = Depends(require_playback),
    ) -> Response:
        _validate_content_id(content_id)
        media_kind = ASSET_KINDS.get(asset_kind)
        if media_kind is None:
            raise HTTPException(status_code=404, detail="媒体对象不存在")
        row = database.get_ready_media_object(content_id=content_id, kind=media_kind)
        if row is None:
            raise HTTPException(status_code=404, detail="内容不存在或尚未就绪")
        payload = _read_verified_object(object_store, row)
        total = len(payload)
        etag = row["etag"]
        headers = {
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Cache-Control": IMMUTABLE_CACHE,
            "Vary": "Authorization",
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
            return Response(
                status_code=status_code,
                headers=headers,
                media_type=row["content_type"],
            )
        return Response(
            content=body,
            status_code=status_code,
            headers=headers,
            media_type=row["content_type"],
        )

    return router


def _validate_content_id(content_id: str) -> None:
    if not CONTENT_ID_PATTERN.fullmatch(content_id):
        raise HTTPException(status_code=404, detail="内容不存在")


def _read_verified_object(
    object_store: FileSystemObjectStore,
    row: sqlite3.Row,
) -> bytes:
    try:
        with object_store.open(row["object_key"]) as source:
            payload = source.read()
    except OSError as error:
        raise HTTPException(status_code=503, detail="媒体对象不可用") from error
    digest = hashlib.sha256(payload).hexdigest()
    if (
        len(payload) != row["byte_length"]
        or digest != row["sha256"]
        or row["etag"] != f'"{digest}"'
    ):
        raise HTTPException(status_code=503, detail="媒体对象校验失败")
    return payload


def _absolutize_manifest_urls(manifest: dict[str, object], base_url: str) -> None:
    playback = manifest.get("playback")
    if not isinstance(playback, dict):
        raise HTTPException(status_code=503, detail="内容清单损坏")
    video = playback.get("video")
    audio = playback.get("audio")
    if not isinstance(video, dict) or not isinstance(audio, dict):
        raise HTTPException(status_code=503, detail="内容清单损坏")
    for descriptor, fields in ((video, ("url",)), (audio, ("url", "index_url"))):
        for field in fields:
            value = descriptor.get(field)
            if not isinstance(value, str) or not value.startswith("/"):
                raise HTTPException(status_code=503, detail="内容清单损坏")
            descriptor[field] = f"{base_url}{value}"
    replay = playback.get("replay")
    if isinstance(replay, dict):
        for field in ("url", "params_url"):
            value = replay.get(field)
            if not isinstance(value, str) or not value.startswith("/"):
                raise HTTPException(status_code=503, detail="内容清单损坏")
            replay[field] = f"{base_url}{value}"


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
