from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, File, HTTPException, Query, Response, Security, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .auth import DeviceTokenService, EmailAlreadyRegistered, UserPrincipal, UserTokenService
from .cloud_processor import CloudMediaProcessor
from .config import Settings
from .content_service import ContentService, EmptySourceAudio, SourceAudioTooLarge
from .database import Database
from .device_api import create_device_router
from .job_repository import ProcessingJobRepository
from .lifecycle import StagingCleanup
from .media_tools import MediaToolError, MediaTools
from .object_store import FileSystemObjectStore
from .openapi_config import error_responses, install_openapi
from .flash_api import create_flash_router
from .landing_page import create_landing_router
from .preview_page import create_preview_router
from .processing_worker import JobProcessor, ProcessingWorker
from .wallet_auth import (
    ChallengeExpired,
    ChallengeNotFound,
    InvalidWalletAddress,
    SignatureMismatch,
    WalletAuthService,
)

try:
    from .chain_service import ChainService
except ImportError:
    ChainService = None

logger = logging.getLogger(__name__)
from .schemas import (
    ChainStatusResponse,
    ContentCreated,
    ContentList,
    ContentLabelUpdate,
    ContentPage,
    ContentSource,
    ContentSummary,
    EditionResponse,
    EditionsListResponse,
    EmailLoginRequest,
    EmailRegisterRequest,
    EmailRegistered,
    HealthResponse,
    MintResultResponse,
    PrepareMintResponse,
    ReadinessResponse,
    SubmitSignedRequest,
    TokenMetadataResponse,
    UserProfile,
    UserTokenIssued,
    WalletChallengeRequest,
    WalletChallengeResponse,
    WalletTokenIssued,
    WalletVerifyRequest,
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
    user_tokens = UserTokenService(database)
    wallet_auth = WalletAuthService(
        database=database,
        user_tokens=user_tokens,
        domain=settings.wallet_auth_domain,
        nonce_ttl_seconds=settings.wallet_nonce_ttl_seconds,
    )
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

    chain_service: ChainService | None = None
    if settings.chain_enabled and settings.chain_contract_address and ChainService is not None:
        try:
            chain_service = ChainService(
                rpc_url=settings.chain_rpc_url,
                chain_id=settings.chain_id,
                contract_address=settings.chain_contract_address,
                operator_key=settings.chain_operator_private_key,
            )
            logger.info("ChainService initialized (contract=%s)", settings.chain_contract_address)
        except Exception as exc:
            logger.warning("ChainService init failed: %s", exc)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        object_store.initialize()
        cleanup.run(now=utc_string(datetime.now(timezone.utc)))
        with database.connect() as conn:
            conn.execute(
                "UPDATE content_chain SET chain_state='FAILED', "
                "error_message='stale MINTING recovered at startup', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE chain_state='MINTING' AND "
                "updated_at < strftime('%Y-%m-%dT%H:%M:%SZ','now','-5 minutes')"
            )
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
            {"name": "users", "description": "Register, log in (email/password or wallet), and issue User Tokens."},
            {"name": "contents", "description": "Upload and manage user-owned sounds."},
            {"name": "chain", "description": "On-chain minting, claiming, and edition management."},
            {"name": "devices", "description": "Trigger resolution and Playback assets."},
        ],
    )
    app.state.settings = settings
    app.state.database = database
    app.state.object_store = object_store
    app.state.media_tools = media_tools
    app.state.user_tokens = user_tokens
    app.state.wallet_auth = wallet_auth
    app.state.device_tokens = device_tokens
    app.state.content_service = content_service
    app.state.job_repository = job_repository
    app.state.processing_worker = processing_worker
    app.state.worker_task = None
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
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
    app.include_router(
        create_preview_router(
            database=database,
            object_store=object_store,
        )
    )
    app.include_router(create_landing_router())
    app.include_router(create_flash_router(database=database))

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
        try:
            database.check()
            object_store.check()
            media_tools.check()
            device_tokens.check_configured()
            worker_task = app.state.worker_task
            if processing_worker is not None and (
                worker_task is None or worker_task.done()
            ):
                raise RuntimeError("processing worker stopped")
        except (OSError, sqlite3.Error, RuntimeError, MediaToolError) as error:
            raise HTTPException(status_code=503, detail="服务依赖尚未就绪") from error
        return ReadinessResponse(
            worker="running" if processing_worker is not None else "disabled"
        )

    @app.post(
        "/api/v1/users",
        response_model=EmailRegistered,
        status_code=201,
        responses=error_responses(409),
        tags=["users"],
        summary="Register with email and password",
        description="Creates a user identity from an email and password. The password is stored only as a PBKDF2 hash.",
        operation_id="registerUser",
    )
    async def register_user(
        payload: EmailRegisterRequest,
        response: Response,
    ) -> EmailRegistered:
        try:
            registered = user_tokens.register_email(
                payload.email,
                payload.password,
                store_private_key=payload.store_private_key,
            )
        except EmailAlreadyRegistered as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        response.headers["Cache-Control"] = "no-store"
        return EmailRegistered(
            user_id=registered.user_id,
            email=payload.email,
            wallet_address=registered.wallet_address,
            private_key=registered.private_key,
            private_key_stored=registered.private_key_stored,
        )

    @app.post(
        "/api/v1/sessions",
        response_model=UserTokenIssued,
        status_code=201,
        responses=error_responses(401),
        tags=["users"],
        summary="Log in with email and password",
        description="Verifies the email and password and issues a fresh opaque User Token returned exactly once.",
        operation_id="loginUser",
    )
    async def login_user(
        payload: EmailLoginRequest,
        response: Response,
    ) -> UserTokenIssued:
        issued = user_tokens.login_email(payload.email, payload.password)
        if issued is None:
            raise HTTPException(
                status_code=401,
                detail="邮箱或密码错误",
                headers={"WWW-Authenticate": "Bearer"},
            )
        response.headers["Cache-Control"] = "no-store"
        return UserTokenIssued(user_id=issued.user_id, token=issued.token)

    @app.post(
        "/api/v1/auth/wallet/challenge",
        response_model=WalletChallengeResponse,
        responses=error_responses(400),
        tags=["users"],
        summary="Request a wallet sign-in challenge",
        description="Returns a nonce-bearing message the Injective wallet must sign via personal_sign.",
        operation_id="requestWalletChallenge",
    )
    async def wallet_challenge(
        payload: WalletChallengeRequest,
        response: Response,
    ) -> WalletChallengeResponse:
        try:
            challenge = wallet_auth.build_challenge(payload.address)
        except InvalidWalletAddress as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        response.headers["Cache-Control"] = "no-store"
        return WalletChallengeResponse(
            address=challenge.address,
            message=challenge.message,
            expires_at=challenge.expires_at,
        )

    @app.post(
        "/api/v1/auth/wallet/verify",
        response_model=WalletTokenIssued,
        status_code=201,
        responses=error_responses(400, 401),
        tags=["users"],
        summary="Verify a wallet signature and issue a token",
        description="Recovers the signer from the EIP-191 signature and issues an opaque User Token bound to the wallet account.",
        operation_id="verifyWalletSignature",
    )
    async def wallet_verify(
        payload: WalletVerifyRequest,
        response: Response,
    ) -> WalletTokenIssued:
        try:
            login = wallet_auth.verify(payload.address, payload.signature)
        except InvalidWalletAddress as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except (ChallengeNotFound, ChallengeExpired, SignatureMismatch) as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        response.headers["Cache-Control"] = "no-store"
        return WalletTokenIssued(
            user_id=login.user_id,
            wallet_address=login.wallet_address,
            token=login.token,
        )

    @app.get(
        "/api/v1/users/me",
        response_model=UserProfile,
        responses=error_responses(401),
        tags=["users"],
        summary="Inspect the authenticated account",
        operation_id="getCurrentUser",
    )
    async def get_current_user(
        response: Response,
        user: UserPrincipal = Depends(require_user),
    ) -> UserProfile:
        row = database.get_user(user.user_id)
        response.headers["Cache-Control"] = "no-store"
        return UserProfile(
            user_id=user.user_id,
            wallet_address=row["wallet_address"] if row is not None else None,
            email=row["email"] if row is not None else None,
        )

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

    @app.post(
        "/api/v1/contents/{content_id}/video",
        status_code=201,
        responses=error_responses(400, 401, 404, 413),
        tags=["contents"],
        summary="Upload visualization video",
        description="Accepts a client-rendered MP4 visualization video and marks the content as READY.",
        operation_id="uploadContentVideo",
    )
    async def upload_video(
        content_id: str,
        response: Response,
        video: Annotated[UploadFile, File(description="MP4 visualization video, maximum 100 MiB")],
        user: UserPrincipal = Depends(require_user),
    ) -> dict[str, str]:
        _require_content_id(content_id)
        row = database.get_owned_content(owner_user_id=user.user_id, content_id=content_id)
        if row is None:
            raise HTTPException(status_code=404, detail="内容不存在")

        data = await video.read()
        if not data:
            raise HTTPException(status_code=400, detail="视频文件为空")
        if len(data) > 100 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="视频文件超过 100 MiB 限制")

        sha256 = hashlib.sha256(data).hexdigest()
        etag = f'"{sha256[:32]}"'
        object_key = f"contents/{content_id}/output/video.mp4"
        object_store.put(object_key, io.BytesIO(data))

        now = utc_string(datetime.now(timezone.utc))
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO media_objects (id, content_id, kind, object_key, content_type, byte_length, sha256, etag, created_at) "
                "VALUES (?, ?, 'VIDEO', ?, 'video/mp4', ?, ?, ?, ?) "
                "ON CONFLICT(content_id, kind) DO UPDATE SET "
                "object_key=?, content_type='video/mp4', byte_length=?, sha256=?, etag=?, created_at=?",
                (uuid.uuid4().hex, content_id, object_key, len(data), sha256, etag, now,
                 object_key, len(data), sha256, etag, now),
            )
            conn.execute(
                "UPDATE contents SET state='READY', ready_at=?, updated_at=? WHERE id=? AND state != 'DELETED'",
                (now, now, content_id),
            )

        response.headers["Cache-Control"] = "no-store"
        return {"content_id": content_id, "state": "READY", "video_sha256": sha256}

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
        "/api/v1/contents/page",
        response_model=ContentPage,
        responses=error_responses(401),
        tags=["contents"],
        summary="List owned content by page",
        operation_id="listOwnedContentsByPage",
    )
    async def list_contents_page(
        response: Response,
        user: UserPrincipal = Depends(require_user),
        page_number: int = Query(ge=1, description="1-based page index"),
        units_per_page: int = Query(ge=1, le=200, description="Items per page"),
    ) -> ContentPage:
        total = database.count_owned_contents(user.user_id)
        offset = (page_number - 1) * units_per_page
        rows = database.list_owned_contents_page(
            user.user_id,
            limit=units_per_page,
            offset=offset,
        )
        total_pages = (total + units_per_page - 1) // units_per_page
        response.headers["Cache-Control"] = "no-store"
        return ContentPage(
            items=[row_to_content_summary(row) for row in rows],
            total=total,
            page_number=page_number,
            units_per_page=units_per_page,
            total_pages=total_pages,
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

    @app.patch(
        "/api/v1/contents/{content_id}",
        response_model=ContentSummary,
        responses=error_responses(401, 404),
        tags=["contents"],
        summary="Rename owned content",
        operation_id="renameOwnedContent",
    )
    async def rename_content(
        content_id: str,
        payload: ContentLabelUpdate,
        response: Response,
        user: UserPrincipal = Depends(require_user),
    ) -> ContentSummary:
        _require_content_id(content_id)
        renamed = database.rename_owned_content(
            owner_user_id=user.user_id,
            content_id=content_id,
            display_label=payload.display_label,
            updated_at=utc_string(datetime.now(timezone.utc)),
        )
        if not renamed:
            raise HTTPException(status_code=404, detail="内容不存在")
        row = database.get_owned_content(
            owner_user_id=user.user_id,
            content_id=content_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="内容不存在")
        response.headers["Cache-Control"] = "no-store"
        return row_to_content_summary(row)

    # ─── Chain helpers ───────────────────────────────────────────────────

    def _require_chain():
        if chain_service is None:
            raise HTTPException(status_code=503, detail="链上服务未启用")

    def _get_content_row(content_id: str) -> sqlite3.Row:
        _require_content_id(content_id)
        with database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM contents WHERE id = ? AND deleted_at IS NULL",
                (content_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="内容不存在")
        return row

    def _get_chain_row(content_id: str) -> sqlite3.Row | None:
        with database.connect() as conn:
            return conn.execute(
                "SELECT * FROM content_chain WHERE content_id = ?",
                (content_id,),
            ).fetchone()

    def _resolve_signing_key(user_row: sqlite3.Row) -> str:
        if settings.chain_operator_private_key:
            return settings.chain_operator_private_key
        user_id = user_row["user_id"]
        with database.connect() as conn:
            pk_row = conn.execute(
                "SELECT private_key FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if pk_row and pk_row["private_key"]:
            return pk_row["private_key"]
        raise HTTPException(status_code=400, detail="无可用签名密钥：运营密钥未配置且用户未存储私钥")

    def _token_uri(content_id: str) -> str:
        return f"{settings.public_base_url}/api/v1/contents/{content_id}/token-metadata"

    # ─── Chain endpoints ─────────────────────────────────────────────────

    @app.get(
        "/api/v1/contents/{content_id}/chain",
        response_model=ChainStatusResponse,
        responses=error_responses(401, 404),
        tags=["chain"],
        summary="Get chain status for content",
        operation_id="getChainStatus",
    )
    def get_chain_status(
        content_id: str,
        user: UserPrincipal = Depends(require_user),
    ) -> ChainStatusResponse:
        _get_content_row(content_id)
        row = _get_chain_row(content_id)
        if row is None:
            return ChainStatusResponse(content_id=content_id, chain_state="NONE")
        return ChainStatusResponse(
            content_id=content_id,
            chain_state=row["chain_state"],
            token_id=row["token_id"],
            tx_hash=row["tx_hash"],
            contract_address=row["contract_address"],
            token_uri=row["token_uri"],
            owner_wallet=row["owner_wallet"],
            error_message=row["error_message"],
            minted_at=row["minted_at"],
        )

    @app.post(
        "/api/v1/contents/{content_id}/chain/mint",
        response_model=MintResultResponse,
        responses=error_responses(400, 401, 404, 409),
        tags=["chain"],
        summary="Mint content on-chain (server-side signing)",
        operation_id="mintContent",
    )
    def mint_content(
        content_id: str,
        user: UserPrincipal = Depends(require_user),
    ) -> MintResultResponse:
        _require_chain()
        content_row = _get_content_row(content_id)
        if content_row["state"] != "READY":
            raise HTTPException(status_code=409, detail="内容尚未处理完成")

        user_row = database.get_user(user.user_id)
        if user_row is None or not user_row["wallet_address"]:
            raise HTTPException(status_code=400, detail="用户无钱包地址")

        signing_key = _resolve_signing_key(user_row)
        wallet = user_row["wallet_address"]
        uri = _token_uri(content_id)
        now = utc_string(datetime.now(timezone.utc))

        chain_row = _get_chain_row(content_id)
        if chain_row and chain_row["chain_state"] == "MINTED":
            return MintResultResponse(
                content_id=content_id,
                token_id=chain_row["token_id"],
                tx_hash=chain_row["tx_hash"],
                contract_address=chain_row["contract_address"],
            )
        if chain_row and chain_row["chain_state"] == "MINTING":
            stale = False
            with database.connect() as conn:
                cur = conn.execute(
                    "SELECT updated_at FROM content_chain WHERE content_id=? AND chain_state='MINTING' "
                    "AND updated_at < strftime('%Y-%m-%dT%H:%M:%SZ','now','-5 minutes')",
                    (content_id,),
                ).fetchone()
                stale = cur is not None
            if not stale:
                raise HTTPException(status_code=409, detail="上链进行中，请稍候")

        with database.connect() as conn:
            conn.execute(
                "INSERT INTO content_chain (content_id, chain_state, owner_wallet, token_uri, created_at, updated_at) "
                "VALUES (?, 'MINTING', ?, ?, ?, ?) "
                "ON CONFLICT(content_id) DO UPDATE SET "
                "chain_state='MINTING', owner_wallet=?, token_uri=?, updated_at=?, error_message=NULL",
                (content_id, wallet, uri, now, now, wallet, uri, now),
            )

        try:
            token_id, tx_hash = chain_service.mint_server_side(wallet, uri, signing_key)
        except Exception as exc:
            with database.connect() as conn:
                conn.execute(
                    "UPDATE content_chain SET chain_state='FAILED', error_message=?, updated_at=? "
                    "WHERE content_id=?",
                    (str(exc)[:500], utc_string(datetime.now(timezone.utc)), content_id),
                )
            raise HTTPException(status_code=500, detail=f"上链失败: {exc}") from exc

        with database.connect() as conn:
            conn.execute(
                "UPDATE content_chain SET chain_state='MINTED', token_id=?, tx_hash=?, "
                "contract_address=?, minted_at=?, updated_at=? WHERE content_id=?",
                (token_id, tx_hash, chain_service.contract_address, now, now, content_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO content_editions (id, content_id, token_id, tx_hash, owner_wallet, token_uri, edition_type, minted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'CREATOR', ?)",
                (uuid.uuid4().hex, content_id, token_id, tx_hash, wallet, uri, now),
            )

        return MintResultResponse(
            content_id=content_id,
            token_id=token_id,
            tx_hash=tx_hash,
            contract_address=chain_service.contract_address,
        )

    @app.post(
        "/api/v1/contents/{content_id}/chain/prepare-mint",
        response_model=PrepareMintResponse,
        responses=error_responses(400, 401, 404, 409),
        tags=["chain"],
        summary="Build unsigned mint tx for client signing",
        operation_id="prepareMint",
    )
    def prepare_mint(
        content_id: str,
        user: UserPrincipal = Depends(require_user),
    ) -> PrepareMintResponse:
        _require_chain()
        content_row = _get_content_row(content_id)
        if content_row["state"] != "READY":
            raise HTTPException(status_code=409, detail="内容尚未处理完成")
        user_row = database.get_user(user.user_id)
        if user_row is None or not user_row["wallet_address"]:
            raise HTTPException(status_code=400, detail="用户无钱包地址")

        chain_row = _get_chain_row(content_id)
        if chain_row and chain_row["chain_state"] == "MINTED":
            raise HTTPException(status_code=409, detail="已上链")

        wallet = user_row["wallet_address"]
        uri = _token_uri(content_id)
        tx = chain_service.build_mint_tx(wallet, uri, wallet)
        return PrepareMintResponse(
            to=tx["to"],
            data=tx["data"],
            nonce=tx["nonce"],
            gas=tx["gas"],
            gas_price=tx["gas_price"],
            chain_id=tx["chain_id"],
            value=tx["value"],
            token_uri=uri,
        )

    @app.post(
        "/api/v1/contents/{content_id}/chain/submit-signed",
        response_model=MintResultResponse,
        responses=error_responses(400, 401, 404, 409),
        tags=["chain"],
        summary="Broadcast client-signed mint tx",
        operation_id="submitSignedMint",
    )
    def submit_signed_mint(
        content_id: str,
        payload: SubmitSignedRequest,
        user: UserPrincipal = Depends(require_user),
    ) -> MintResultResponse:
        _require_chain()
        _get_content_row(content_id)
        user_row = database.get_user(user.user_id)
        if user_row is None or not user_row["wallet_address"]:
            raise HTTPException(status_code=400, detail="用户无钱包地址")

        wallet = user_row["wallet_address"]
        uri = _token_uri(content_id)
        now = utc_string(datetime.now(timezone.utc))

        try:
            tx_hash = chain_service.send_raw_tx(payload.raw_tx)
            token_id = chain_service.get_token_id_from_receipt(tx_hash)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"交易广播失败: {exc}") from exc

        with database.connect() as conn:
            conn.execute(
                "INSERT INTO content_chain (content_id, chain_state, token_id, tx_hash, contract_address, owner_wallet, token_uri, minted_at, created_at, updated_at) "
                "VALUES (?, 'MINTED', ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(content_id) DO UPDATE SET "
                "chain_state='MINTED', token_id=?, tx_hash=?, contract_address=?, minted_at=?, updated_at=?, error_message=NULL",
                (content_id, token_id, tx_hash, chain_service.contract_address, wallet, uri, now, now, now,
                 token_id, tx_hash, chain_service.contract_address, now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO content_editions (id, content_id, token_id, tx_hash, owner_wallet, token_uri, edition_type, minted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'CREATOR', ?)",
                (uuid.uuid4().hex, content_id, token_id, tx_hash, wallet, uri, now),
            )

        return MintResultResponse(
            content_id=content_id,
            token_id=token_id,
            tx_hash=tx_hash,
            contract_address=chain_service.contract_address,
        )

    @app.post(
        "/api/v1/contents/{content_id}/claim",
        response_model=MintResultResponse,
        responses=error_responses(400, 401, 404, 409),
        tags=["chain"],
        summary="Claim an edition (server-side signing)",
        operation_id="claimContent",
    )
    def claim_content(
        content_id: str,
        user: UserPrincipal = Depends(require_user),
    ) -> MintResultResponse:
        _require_chain()
        _get_content_row(content_id)
        user_row = database.get_user(user.user_id)
        if user_row is None or not user_row["wallet_address"]:
            raise HTTPException(status_code=400, detail="用户无钱包地址")

        wallet = user_row["wallet_address"]
        chain_row = _get_chain_row(content_id)
        if not chain_row or chain_row["chain_state"] != "MINTED":
            raise HTTPException(status_code=409, detail="内容尚未上链，无法领取")

        with database.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM content_editions WHERE content_id=? AND owner_wallet=?",
                (content_id, wallet),
            ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="已领取过该内容")

        signing_key = _resolve_signing_key(user_row)
        uri = _token_uri(content_id)
        now = utc_string(datetime.now(timezone.utc))

        try:
            token_id, tx_hash = chain_service.mint_server_side(wallet, uri, signing_key)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"领取上链失败: {exc}") from exc

        with database.connect() as conn:
            conn.execute(
                "INSERT INTO content_editions (id, content_id, token_id, tx_hash, owner_wallet, token_uri, edition_type, minted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'CLAIM', ?)",
                (uuid.uuid4().hex, content_id, token_id, tx_hash, wallet, uri, now),
            )

        return MintResultResponse(
            content_id=content_id,
            token_id=token_id,
            tx_hash=tx_hash,
            contract_address=chain_service.contract_address,
        )

    @app.post(
        "/api/v1/contents/{content_id}/claim/prepare",
        response_model=PrepareMintResponse,
        responses=error_responses(400, 401, 404, 409),
        tags=["chain"],
        summary="Build unsigned claim tx for client signing",
        operation_id="prepareClaim",
    )
    def prepare_claim(
        content_id: str,
        user: UserPrincipal = Depends(require_user),
    ) -> PrepareMintResponse:
        _require_chain()
        _get_content_row(content_id)
        user_row = database.get_user(user.user_id)
        if user_row is None or not user_row["wallet_address"]:
            raise HTTPException(status_code=400, detail="用户无钱包地址")

        wallet = user_row["wallet_address"]
        chain_row = _get_chain_row(content_id)
        if not chain_row or chain_row["chain_state"] != "MINTED":
            raise HTTPException(status_code=409, detail="内容尚未上链，无法领取")

        with database.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM content_editions WHERE content_id=? AND owner_wallet=?",
                (content_id, wallet),
            ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="已领取过该内容")

        uri = _token_uri(content_id)
        tx = chain_service.build_mint_tx(wallet, uri, wallet)
        return PrepareMintResponse(
            to=tx["to"],
            data=tx["data"],
            nonce=tx["nonce"],
            gas=tx["gas"],
            gas_price=tx["gas_price"],
            chain_id=tx["chain_id"],
            value=tx["value"],
            token_uri=uri,
        )

    @app.post(
        "/api/v1/contents/{content_id}/claim/submit-signed",
        response_model=MintResultResponse,
        responses=error_responses(400, 401, 404, 409),
        tags=["chain"],
        summary="Broadcast client-signed claim tx",
        operation_id="submitSignedClaim",
    )
    def submit_signed_claim(
        content_id: str,
        payload: SubmitSignedRequest,
        user: UserPrincipal = Depends(require_user),
    ) -> MintResultResponse:
        _require_chain()
        _get_content_row(content_id)
        user_row = database.get_user(user.user_id)
        if user_row is None or not user_row["wallet_address"]:
            raise HTTPException(status_code=400, detail="用户无钱包地址")

        wallet = user_row["wallet_address"]
        uri = _token_uri(content_id)
        now = utc_string(datetime.now(timezone.utc))

        try:
            tx_hash = chain_service.send_raw_tx(payload.raw_tx)
            token_id = chain_service.get_token_id_from_receipt(tx_hash)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"交易广播失败: {exc}") from exc

        with database.connect() as conn:
            conn.execute(
                "INSERT INTO content_editions (id, content_id, token_id, tx_hash, owner_wallet, token_uri, edition_type, minted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'CLAIM', ?)",
                (uuid.uuid4().hex, content_id, token_id, tx_hash, wallet, uri, now),
            )

        return MintResultResponse(
            content_id=content_id,
            token_id=token_id,
            tx_hash=tx_hash,
            contract_address=chain_service.contract_address,
        )

    @app.get(
        "/api/v1/contents/{content_id}/token-metadata",
        response_model=TokenMetadataResponse,
        tags=["chain"],
        summary="ERC-721 token metadata (public)",
        operation_id="getTokenMetadata",
    )
    def token_metadata(content_id: str) -> TokenMetadataResponse:
        _require_content_id(content_id)
        with database.connect() as conn:
            row = conn.execute(
                "SELECT display_label, duration_ms, created_at FROM contents WHERE id=? AND deleted_at IS NULL",
                (content_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="内容不存在")
        return TokenMetadataResponse(
            name=f"SoundPola: {row['display_label']}",
            description=f"Sound memory #{content_id[:8]} - {row['duration_ms']}ms",
            image=f"{settings.public_base_url}/api/v1/contents/{content_id}/assets/video",
            attributes=[
                {"trait_type": "Duration", "value": f"{row['duration_ms']}ms"},
                {"trait_type": "Content ID", "value": content_id},
                {"trait_type": "Created", "value": row["created_at"]},
            ],
        )

    @app.get(
        "/api/v1/contents/{content_id}/editions",
        response_model=EditionsListResponse,
        responses=error_responses(401, 404),
        tags=["chain"],
        summary="List all editions for content",
        operation_id="listEditions",
    )
    def list_editions(
        content_id: str,
        user: UserPrincipal = Depends(require_user),
    ) -> EditionsListResponse:
        _get_content_row(content_id)
        with database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM content_editions WHERE content_id=? ORDER BY minted_at",
                (content_id,),
            ).fetchall()
        return EditionsListResponse(
            content_id=content_id,
            editions=[
                EditionResponse(
                    id=r["id"],
                    content_id=r["content_id"],
                    token_id=r["token_id"],
                    tx_hash=r["tx_hash"],
                    owner_wallet=r["owner_wallet"],
                    token_uri=r["token_uri"],
                    edition_type=r["edition_type"],
                    minted_at=r["minted_at"],
                )
                for r in rows
            ],
        )

    install_openapi(app, public_base_url=settings.public_base_url)
    return app


def _require_content_id(content_id: str) -> None:
    if not CONTENT_ID_PATTERN.fullmatch(content_id):
        raise HTTPException(status_code=404, detail="内容不存在")


app = create_app()
