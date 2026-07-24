from __future__ import annotations

import asyncio
import hmac
import json
import mimetypes
import os
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from .auth import DeviceTokenService, UserPrincipal, UserTokenService
from .config import Settings
from .content_service import ContentService, EmptySourceAudio, SourceAudioTooLarge
from .database import Database
from .device_api import create_device_router
from .job_repository import ProcessingJobRepository
from .media_tools import MediaToolError, MediaTools
from .object_store import FileSystemObjectStore
from .processing_worker import JobProcessor, ProcessingWorker
from .schemas import (
    ContentCreated,
    ContentList,
    ContentSource,
    ContentSummary,
    PackageCreated,
    PackageList,
    PackageMetadata,
    PackageSummary,
    UserTokenIssued,
)
from .storage import FileTooLarge, build_bundle, iter_file, remove_tree, save_upload, write_manifest
from .validation import InvalidMedia, validate_audio, validate_stl, validate_webm


RANGE_PATTERN = re.compile(r"bytes=(\d*)-(\d*)$")
PACKAGE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def utc_string(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_metadata(raw: str) -> PackageMetadata:
    if len(raw.encode("utf-8")) > 64 * 1024:
        raise ValueError("metadata 超过 64 KiB")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"metadata 不是有效 JSON：{error.msg}") from error
    try:
        validator = getattr(PackageMetadata, "model_validate", None)
        metadata = validator(data) if validator else PackageMetadata.parse_obj(data)
    except ValidationError as error:
        raise ValueError(f"metadata 校验失败：{error}") from error

    cleaned_tags: list[str] = []
    for tag in metadata.tags:
        tag = tag.strip()
        if not tag or len(tag) > 64:
            raise ValueError("标签不能为空且长度不能超过 64")
        if tag not in cleaned_tags:
            cleaned_tags.append(tag)
    metadata.tags = cleaned_tags
    encoded_custom = json.dumps(metadata.custom_metadata, ensure_ascii=False)
    if len(encoded_custom.encode("utf-8")) > 64 * 1024:
        raise ValueError("custom_metadata 超过 64 KiB")
    return metadata


def parse_range(value: str, size: int) -> tuple[int, int] | None:
    match = RANGE_PATTERN.fullmatch(value.strip())
    if not match or size <= 0:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:
        suffix_length = int(last)
        if suffix_length <= 0:
            return None
        return max(0, size - suffix_length), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


def row_to_content_summary(row: sqlite3.Row) -> ContentSummary:
    content_id = row["id"]
    return ContentSummary(
        content_id=content_id,
        state=row["state"],
        processing_stage=row["processing_stage"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        display_label=row["display_label"],
        duration_ms=row["duration_ms"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        ready_at=row["ready_at"],
        deleted_at=row["deleted_at"],
        status_url=f"/api/v1/contents/{content_id}",
        nfc_url=(
            f"/c/{content_id}"
            if row["state"] == "READY" and row["deleted_at"] is None
            else None
        ),
        source=ContentSource(
            filename=row["source_filename"],
            content_type=row["source_content_type"],
            byte_length=row["source_byte_length"],
            sha256=row["source_sha256"],
        ),
    )


def row_to_summary(row: sqlite3.Row) -> PackageSummary:
    return PackageSummary(
        package_id=row["id"],
        title=row["title"],
        description=row["description"],
        creator={"id": row["creator_id"], "name": row["creator_name"]},
        created_at=row["created_at"],
        received_at=row["received_at"],
        status=row["status"],
        tags=json.loads(row["tags_json"]),
        total_size=row["total_size"],
    )


def create_app(
    settings: Settings | None = None,
    *,
    job_processor: JobProcessor | None = None,
) -> FastAPI:
    settings = settings or Settings()
    database = Database(settings.database_path)
    object_store = FileSystemObjectStore(
        objects_root=settings.object_store_dir,
        staging_root=settings.object_staging_dir,
        chunk_size=settings.chunk_size,
    )
    media_tools = MediaTools(
        ffmpeg_binary=settings.ffmpeg_binary,
        ffprobe_binary=settings.ffprobe_binary,
        timeout_seconds=settings.media_command_timeout_seconds,
    )
    user_tokens = UserTokenService(database)
    device_tokens = DeviceTokenService(
        trigger_token=settings.trigger_token,
        playback_token=settings.playback_token,
    )
    content_service = ContentService(
        database=database,
        object_store=object_store,
        max_audio_bytes=settings.max_audio_bytes,
        chunk_size=settings.chunk_size,
    )
    job_repository = ProcessingJobRepository(database)
    processing_worker = (
        ProcessingWorker(
            repository=job_repository,
            processor=job_processor,
            lease_seconds=settings.job_lease_seconds,
            poll_seconds=settings.worker_poll_seconds,
        )
        if job_processor is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.packages_dir.mkdir(parents=True, exist_ok=True)
        settings.staging_dir.mkdir(parents=True, exist_ok=True)
        database.initialize()
        object_store.initialize()
        worker_task: asyncio.Task[None] | None = None
        if processing_worker is not None:
            worker_task = asyncio.create_task(
                processing_worker.run_forever(),
                name="cloud-media-processing-worker",
            )
            app.state.worker_task = worker_task
        try:
            yield
        finally:
            if processing_worker is not None and worker_task is not None:
                processing_worker.stop()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(worker_task),
                        timeout=max(1.0, settings.worker_poll_seconds * 2),
                    )
                except TimeoutError:
                    worker_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker_task

    app = FastAPI(
        title="Multimedia Package Backend",
        version="1.0.0",
        description="接收、存储并分发音频、STL、WebM 和 JSON 元数据包。",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.object_store = object_store
    app.state.media_tools = media_tools
    app.state.user_tokens = user_tokens
    app.state.device_tokens = device_tokens
    app.state.content_service = content_service
    app.state.job_repository = job_repository
    app.state.processing_worker = processing_worker
    app.state.worker_task = None
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=settings.allowed_origins != ["*"],
        allow_methods=["GET", "HEAD", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Range", "If-Range"],
        expose_headers=[
            "Accept-Ranges",
            "Content-Range",
            "Content-Length",
            "ETag",
            "Cache-Control",
            "Vary",
        ],
    )
    app.include_router(
        create_device_router(
            database=database,
            object_store=object_store,
            tokens=device_tokens,
        )
    )

    async def require_write_token(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if device_tokens.authenticate(authorization) is not None:
            raise HTTPException(status_code=403, detail="设备 Token 无权写入内容")
        if not settings.api_token:
            return
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, settings.api_token):
            raise HTTPException(status_code=401, detail="无效或缺少 Bearer Token")

    async def require_user(
        authorization: Annotated[str | None, Header()] = None,
    ) -> UserPrincipal:
        principal = user_tokens.authenticate(authorization)
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail="无效或缺少用户 Token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return principal

    def get_row(package_id: str) -> sqlite3.Row:
        if not PACKAGE_ID_PATTERN.fullmatch(package_id):
            raise HTTPException(status_code=404, detail="数据包不存在")
        row = database.get_package(package_id)
        if row is None:
            raise HTTPException(status_code=404, detail="数据包不存在")
        return row

    def load_manifest(row: sqlite3.Row) -> dict[str, Any]:
        path = settings.packages_dir / row["storage_path"] / "manifest.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=500, detail="数据包 manifest 损坏") from error

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/ready")
    async def ready() -> dict[str, str]:
        try:
            database.check()
            probe = settings.staging_dir / f".write-probe-{uuid.uuid4().hex}"
            probe.write_bytes(b"ok")
            probe.unlink()
            object_store.check()
            media_tools.check()
            device_tokens.check_configured()
            worker_task = app.state.worker_task
            if worker_task is not None and worker_task.done():
                raise RuntimeError("processing worker stopped")
        except (OSError, sqlite3.Error, RuntimeError, MediaToolError) as error:
            raise HTTPException(status_code=503, detail="存储或数据库不可用") from error
        return {"status": "ready"}

    @app.post(
        "/api/v1/users/tokens",
        response_model=UserTokenIssued,
        status_code=201,
    )
    async def issue_user_token(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> UserTokenIssued:
        if device_tokens.authenticate(authorization) is not None:
            raise HTTPException(status_code=403, detail="设备 Token 无权签发用户 Token")
        issued = user_tokens.issue()
        response.headers["Cache-Control"] = "no-store"
        return UserTokenIssued(user_id=issued.user_id, token=issued.token)

    @app.post(
        "/api/v1/contents",
        response_model=ContentCreated,
        status_code=201,
    )
    async def create_content(
        response: Response,
        audio: Annotated[UploadFile, File()],
        user: UserPrincipal = Depends(require_user),
    ) -> ContentCreated:
        try:
            created = await content_service.upload(
                owner_user_id=user.user_id,
                audio=audio,
            )
        except SourceAudioTooLarge as error:
            raise HTTPException(status_code=413, detail=str(error)) from error
        except EmptySourceAudio as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        response.headers["Cache-Control"] = "no-store"
        return ContentCreated(
            content_id=created.content_id,
            state=created.state,
            display_label=created.display_label,
            status_url=f"/api/v1/contents/{created.content_id}",
        )

    @app.get("/api/v1/contents", response_model=ContentList)
    async def list_contents(
        response: Response,
        user: UserPrincipal = Depends(require_user),
    ) -> ContentList:
        rows = database.list_owned_contents(user.user_id)
        response.headers["Cache-Control"] = "no-store"
        return ContentList(
            items=[row_to_content_summary(row) for row in rows],
            total=len(rows),
        )

    @app.get("/api/v1/contents/{content_id}", response_model=ContentSummary)
    async def get_content(
        content_id: str,
        response: Response,
        user: UserPrincipal = Depends(require_user),
    ) -> ContentSummary:
        if not PACKAGE_ID_PATTERN.fullmatch(content_id):
            raise HTTPException(status_code=404, detail="内容不存在")
        row = database.get_owned_content(
            owner_user_id=user.user_id,
            content_id=content_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="内容不存在")
        response.headers["Cache-Control"] = "no-store"
        return row_to_content_summary(row)

    @app.post(
        "/api/v1/contents/{content_id}/retry",
        response_model=ContentSummary,
        status_code=202,
    )
    async def retry_content(
        content_id: str,
        response: Response,
        user: UserPrincipal = Depends(require_user),
    ) -> ContentSummary:
        if not PACKAGE_ID_PATTERN.fullmatch(content_id):
            raise HTTPException(status_code=404, detail="内容不存在")
        result = database.retry_owned_content(
            owner_user_id=user.user_id,
            content_id=content_id,
            updated_at=utc_string(datetime.now(timezone.utc)),
        )
        if result is None:
            raise HTTPException(status_code=404, detail="内容不存在")
        if result == "NOT_FAILED":
            raise HTTPException(status_code=409, detail="内容当前不可重试")
        if result == "TERMINAL":
            raise HTTPException(status_code=409, detail="源文件错误不可重试，请重新上传")
        row = database.get_owned_content(
            owner_user_id=user.user_id,
            content_id=content_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="内容不存在")
        response.headers["Cache-Control"] = "no-store"
        return row_to_content_summary(row)

    @app.delete("/api/v1/contents/{content_id}", status_code=204)
    async def delete_content(
        content_id: str,
        user: UserPrincipal = Depends(require_user),
    ) -> Response:
        if not PACKAGE_ID_PATTERN.fullmatch(content_id):
            raise HTTPException(status_code=404, detail="内容不存在")
        deleted = database.delete_owned_content(
            owner_user_id=user.user_id,
            content_id=content_id,
            deleted_at=utc_string(datetime.now(timezone.utc)),
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="内容不存在")
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.post(
        "/api/v1/packages",
        response_model=PackageCreated,
        status_code=201,
        dependencies=[Depends(require_write_token)],
    )
    async def create_package(
        metadata: Annotated[str, Form()],
        audio: Annotated[UploadFile, File()],
        model: Annotated[UploadFile, File()],
        video: Annotated[UploadFile, File()],
    ) -> PackageCreated:
        package_id = uuid.uuid4().hex
        staging = settings.staging_dir / package_id
        final = settings.packages_dir / package_id
        committed = False
        try:
            parsed = parse_metadata(metadata)
            audio_name = audio.filename or "audio"
            model_name = model.filename or "model.stl"
            video_name = video.filename or "video.webm"
            if Path(model_name).suffix.lower() != ".stl":
                raise InvalidMedia("3D 模型必须使用 .stl 扩展名")
            if Path(video_name).suffix.lower() != ".webm":
                raise InvalidMedia("视频必须使用 .webm 扩展名")

            staging.mkdir(parents=False, exist_ok=False)
            audio_temporary = staging / "audio.upload"
            model_path = staging / "model.stl"
            video_path = staging / "video.webm"
            audio_size, audio_hash = await save_upload(
                audio,
                audio_temporary,
                max_bytes=settings.max_audio_bytes,
                chunk_size=settings.chunk_size,
            )
            model_size, model_hash = await save_upload(
                model,
                model_path,
                max_bytes=settings.max_model_bytes,
                chunk_size=settings.chunk_size,
            )
            video_size, video_hash = await save_upload(
                video,
                video_path,
                max_bytes=settings.max_video_bytes,
                chunk_size=settings.chunk_size,
            )
            audio_suffix = validate_audio(audio_temporary, audio_name)
            validate_stl(model_path)
            validate_webm(video_path)
            audio_filename = f"audio{audio_suffix}"
            audio_path = staging / audio_filename
            os.replace(audio_temporary, audio_path)

            received_at = datetime.now(timezone.utc)
            created_at = parsed.created_at or received_at
            manifest = {
                "schema_version": "1.0",
                "package_id": package_id,
                "title": parsed.title,
                "description": parsed.description,
                "creator": {"id": parsed.creator.id, "name": parsed.creator.name},
                "created_at": utc_string(created_at),
                "received_at": utc_string(received_at),
                "status": "ready",
                "tags": parsed.tags,
                "files": {
                    "audio": {
                        "filename": audio_filename,
                        "original_filename": Path(audio_name).name,
                        "content_type": mimetypes.guess_type(audio_filename)[0] or "application/octet-stream",
                        "size": audio_size,
                        "sha256": audio_hash,
                    },
                    "model": {
                        "filename": "model.stl",
                        "original_filename": Path(model_name).name,
                        "content_type": "model/stl",
                        "size": model_size,
                        "sha256": model_hash,
                    },
                    "video": {
                        "filename": "video.webm",
                        "original_filename": Path(video_name).name,
                        "content_type": "video/webm",
                        "size": video_size,
                        "sha256": video_hash,
                    },
                },
                "custom_metadata": parsed.custom_metadata,
            }
            write_manifest(staging / "manifest.json", manifest)
            os.replace(staging, final)
            committed = True
            database.insert_package(
                {
                    "id": package_id,
                    "schema_version": "1.0",
                    "title": parsed.title,
                    "description": parsed.description,
                    "creator_id": parsed.creator.id,
                    "creator_name": parsed.creator.name,
                    "created_at": manifest["created_at"],
                    "received_at": manifest["received_at"],
                    "status": "ready",
                    "tags_json": json.dumps(parsed.tags, ensure_ascii=False),
                    "custom_metadata_json": json.dumps(parsed.custom_metadata, ensure_ascii=False),
                    "storage_path": package_id,
                    "audio_filename": audio_filename,
                    "audio_size": audio_size,
                    "audio_sha256": audio_hash,
                    "model_filename": "model.stl",
                    "model_size": model_size,
                    "model_sha256": model_hash,
                    "video_filename": "video.webm",
                    "video_size": video_size,
                    "video_sha256": video_hash,
                    "total_size": audio_size + model_size + video_size,
                }
            )
        except FileTooLarge as error:
            remove_tree(final if committed else staging)
            raise HTTPException(status_code=413, detail=str(error)) from error
        except (ValueError, InvalidMedia) as error:
            remove_tree(final if committed else staging)
            raise HTTPException(status_code=400, detail=str(error)) from error
        except Exception:
            remove_tree(final if committed else staging)
            raise
        finally:
            await audio.close()
            await model.close()
            await video.close()
        return PackageCreated(
            package_id=package_id,
            status="ready",
            manifest_url=f"/api/v1/packages/{package_id}/manifest",
        )

    @app.get("/api/v1/packages", response_model=PackageList)
    async def list_packages(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        creator_id: str | None = None,
        status: str | None = None,
        tag: str | None = None,
    ) -> PackageList:
        rows, total = database.list_packages(
            limit=limit,
            offset=offset,
            creator_id=creator_id,
            status=status,
            tag=tag,
        )
        return PackageList(
            items=[row_to_summary(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/v1/packages/{package_id}")
    async def get_package(package_id: str) -> JSONResponse:
        return JSONResponse(load_manifest(get_row(package_id)))

    @app.get("/api/v1/packages/{package_id}/manifest")
    async def get_manifest(package_id: str) -> JSONResponse:
        return JSONResponse(load_manifest(get_row(package_id)))

    @app.api_route("/api/v1/packages/{package_id}/files/{kind}", methods=["GET", "HEAD"])
    async def get_file(package_id: str, kind: str, request: Request) -> Response:
        if kind not in {"audio", "model", "video"}:
            raise HTTPException(status_code=404, detail="文件类型不存在")
        row = get_row(package_id)
        manifest = load_manifest(row)
        file_info = manifest["files"][kind]
        path = settings.packages_dir / row["storage_path"] / file_info["filename"]
        if not path.is_file():
            raise HTTPException(status_code=500, detail="数据文件丢失")
        size = path.stat().st_size
        etag = f'"sha256-{file_info["sha256"]}"'
        base_headers = {
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Content-Disposition": f'inline; filename="{file_info["filename"]}"',
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=base_headers)

        start, end, status_code = 0, size - 1, 200
        range_header = request.headers.get("range")
        if range_header:
            parsed_range = parse_range(range_header, size)
            if parsed_range is None:
                return Response(
                    status_code=416,
                    headers={**base_headers, "Content-Range": f"bytes */{size}"},
                )
            start, end = parsed_range
            status_code = 206
            base_headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = end - start + 1
        base_headers["Content-Length"] = str(length)
        if request.method == "HEAD":
            return Response(
                status_code=status_code,
                headers=base_headers,
                media_type=file_info["content_type"],
            )
        return StreamingResponse(
            iter_file(path, start, length, settings.chunk_size),
            status_code=status_code,
            headers=base_headers,
            media_type=file_info["content_type"],
        )

    @app.get("/api/v1/packages/{package_id}/bundle")
    async def get_bundle(package_id: str) -> StreamingResponse:
        row = get_row(package_id)
        manifest = load_manifest(row)
        package_dir = settings.packages_dir / row["storage_path"]
        filenames = ["manifest.json", *(item["filename"] for item in manifest["files"].values())]
        archive = build_bundle(package_dir, filenames)
        archive.seek(0, os.SEEK_END)
        size = archive.tell()
        archive.seek(0)

        def stream_archive():
            try:
                while chunk := archive.read(settings.chunk_size):
                    yield chunk
            finally:
                archive.close()

        return StreamingResponse(
            stream_archive(),
            media_type="application/zip",
            headers={
                "Content-Length": str(size),
                "Content-Disposition": f'attachment; filename="{package_id}.zip"',
            },
        )

    @app.delete(
        "/api/v1/packages/{package_id}",
        status_code=204,
        dependencies=[Depends(require_write_token)],
    )
    async def delete_package(package_id: str) -> Response:
        get_row(package_id)
        if not database.mark_deleted(package_id):
            raise HTTPException(status_code=404, detail="数据包不存在")
        return Response(status_code=204)

    return app


app = create_app()
