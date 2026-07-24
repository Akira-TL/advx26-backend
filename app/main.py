from __future__ import annotations

import asyncio
import re
import sqlite3
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Annotated, Callable

from fastapi import Depends, FastAPI, File, Header, HTTPException, Response, Security, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .auth import DeviceTokenService, UserPrincipal, UserTokenService
from .cloud_processor import CloudMediaProcessor
from .config import Settings
from .content_service import ContentService, EmptySourceAudio, SourceAudioTooLarge
from .database import Database
from .device_api import create_device_router
from .frame_renderer import HeadlessFrameRenderer
from .job_repository import ProcessingJobRepository
from .lifecycle import StagingCleanup
from .media_tools import MediaTools
from .object_store import FileSystemObjectStore
from .openapi_config import error_responses, install_openapi
from .processing_worker import JobProcessor, ProcessingWorker
from .schemas import (
    ContentCreated,
    ContentList,
    ContentSource,
    ContentSummary,
    HealthResponse,
    ReadinessResponse,
    UserTokenIssued,
)


CONTENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def utc_string(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
    frame_renderer = HeadlessFrameRenderer(
        object_store=object_store,
        project_dir=settings.renderer_project_dir,
        node_binary=settings.node_binary,
        timeout_seconds=settings.renderer_timeout_seconds,
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
        max_attempts=settings.processing_max_attempts,
    )
    job_repository = ProcessingJobRepository(database)
    uses_default_processor = job_processor is None and settings.worker_enabled
    processor = job_processor
    if uses_default_processor:
        processor = CloudMediaProcessor(
            database=database,
            object_store=object_store,
            media_tools=media_tools,
            frame_renderer=frame_renderer,
        )
    processing_worker = (
        ProcessingWorker(
            repository=job_repository,
            processor=processor,
            lease_seconds=settings.job_lease_seconds,
            poll_seconds=settings.worker_poll_seconds,
        )
        if processor is not None
        else None
    )
    cleanup = StagingCleanup(
        database=database,
        object_store=object_store,
        failed_retention_seconds=settings.failed_staging_retention_seconds,
        failed_max_bytes=settings.failed_staging_max_bytes,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        object_store.initialize()
        cleanup.run(now=utc_string(datetime.now(timezone.utc)))
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
        title="AdventureX Cloud Media Service",
        summary="User-owned audio ingestion and deterministic NFC media playback.",
        version="2.0.0",
        description=(
            "Authenticate users and fixed devices, normalize uploaded audio, render the "
            "Sound Visualization, publish immutable H.264/MP3 media, and serve NFC playback."
        ),
        lifespan=lifespan,
        servers=[
            {
                "url": settings.public_base_url,
                "description": "Configured public Cloud Media Service endpoint.",
            }
        ],
        openapi_tags=[
            {"name": "operations", "description": "Health and deployment readiness."},
            {"name": "users", "description": "Issue long-lived opaque User Tokens."},
            {"name": "contents", "description": "Upload and manage user-owned sounds."},
            {"name": "devices", "description": "Trigger resolution and Playback assets."},
        ],
    )
    app.state.settings = settings
    app.state.database = database
    app.state.object_store = object_store
    app.state.media_tools = media_tools
    app.state.frame_renderer = frame_renderer
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

    user_scheme = HTTPBearer(
        auto_error=False,
        scheme_name="UserToken",
        description="Long-lived opaque Bearer Token returned once by the issuance endpoint.",
    )

    async def require_user(
        credentials: HTTPAuthorizationCredentials | None = Security(user_scheme),
    ) -> UserPrincipal:
        authorization = (
            f"{credentials.scheme} {credentials.credentials}"
            if credentials is not None
            else None
        )
        principal = user_tokens.authenticate(authorization)
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail="无效或缺少用户 Token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return principal

    @app.get(
        "/api/v1/health",
        response_model=HealthResponse,
        tags=["operations"],
        summary="Process liveness",
        description="Returns 200 when the HTTP process can answer requests; it does not prove dependencies are ready.",
        operation_id="getHealth",
    )
    async def health() -> HealthResponse:
        return HealthResponse()

    @app.get(
        "/api/v1/ready",
        response_model=ReadinessResponse,
        responses=error_responses(503),
        tags=["operations"],
        summary="Deployment readiness",
        description=(
            "Checks SQLite, both Object Store roots, FFmpeg, FFprobe, fixed device Tokens, "
            "and—when enabled—the Node/Puppeteer renderer and worker task."
        ),
        operation_id="getReadiness",
    )
    async def ready() -> ReadinessResponse:
        checks: list[tuple[str, Callable[[], None]]] = [
            ("database", database.check),
            ("object_store", object_store.check),
            ("media_tools", media_tools.check),
            ("device_tokens", device_tokens.check_configured),
        ]
        if uses_default_processor:
            checks.append(("renderer", frame_renderer.check))

        for component, check in checks:
            try:
                check()
            except Exception as error:
                message = str(error).strip() or type(error).__name__
                raise HTTPException(
                    status_code=503,
                    detail={
                        "component": component,
                        "error": message,
                        "exception": type(error).__name__,
                    },
                ) from error

        worker_task = app.state.worker_task
        if processing_worker is not None and (
            worker_task is None or worker_task.done()
        ):
            error = RuntimeError("processing worker stopped")
            raise HTTPException(
                status_code=503,
                detail={
                    "component": "worker",
                    "error": str(error),
                    "exception": type(error).__name__,
                },
            ) from error

        return ReadinessResponse(
            worker="running" if processing_worker is not None else "disabled"
        )

    @app.post(
        "/api/v1/users/tokens",
        response_model=UserTokenIssued,
        status_code=201,
        responses=error_responses(403),
        tags=["users"],
        summary="Issue a User Token",
        description="Creates a minimal user identity and returns its opaque Token exactly once.",
        operation_id="issueUserToken",
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
        responses=error_responses(400, 401, 413),
        tags=["contents"],
        summary="Upload source audio",
        description="Stores the exact original audio and queues deterministic cloud media processing.",
        operation_id="uploadContent",
    )
    async def create_content(
        response: Response,
        audio: Annotated[UploadFile, File(description="One source audio file, maximum 50 MiB")],
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

    @app.get(
        "/api/v1/contents",
        response_model=ContentList,
        responses=error_responses(401),
        tags=["contents"],
        summary="List owned content",
        operation_id="listOwnedContents",
    )
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

    @app.get(
        "/api/v1/contents/{content_id}",
        response_model=ContentSummary,
        responses=error_responses(401, 404),
        tags=["contents"],
        summary="Inspect owned content",
        operation_id="getOwnedContent",
    )
    async def get_content(
        content_id: str,
        response: Response,
        user: UserPrincipal = Depends(require_user),
    ) -> ContentSummary:
        _require_content_id(content_id)
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
        responses=error_responses(401, 404, 409),
        tags=["contents"],
        summary="Retry eligible failed content",
        operation_id="retryOwnedContent",
    )
    async def retry_content(
        content_id: str,
        response: Response,
        user: UserPrincipal = Depends(require_user),
    ) -> ContentSummary:
        _require_content_id(content_id)
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

    @app.delete(
        "/api/v1/contents/{content_id}",
        status_code=204,
        responses=error_responses(401, 404),
        tags=["contents"],
        summary="Revoke owned content",
        description="Immediately revokes device access; generated objects are cleaned later.",
        operation_id="deleteOwnedContent",
    )
    async def delete_content(
        content_id: str,
        user: UserPrincipal = Depends(require_user),
    ) -> Response:
        _require_content_id(content_id)
        deleted = database.delete_owned_content(
            owner_user_id=user.user_id,
            content_id=content_id,
            deleted_at=utc_string(datetime.now(timezone.utc)),
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="内容不存在")
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    install_openapi(app, public_base_url=settings.public_base_url)
    return app


def _require_content_id(content_id: str) -> None:
    if not CONTENT_ID_PATTERN.fullmatch(content_id):
        raise HTTPException(status_code=404, detail="内容不存在")


app = create_app()
