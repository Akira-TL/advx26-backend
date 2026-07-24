from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import datetime, timezone
from typing import Protocol

from .job_repository import ClaimedJob, ProcessingJobRepository


PROCESSING_STAGES = frozenset(
    {
        "PROBING",
        "NORMALIZING_AUDIO",
        "BUILDING_AUDIO_INDEX",
        "BUILDING_FEATURE_TIMELINE",
        "RENDERING_VIDEO",
        "ENCODING_VIDEO",
        "VALIDATING",
    }
)


class JobProcessor(Protocol):
    async def process(self, job: ClaimedJob, reporter: "StageReporter") -> None: ...


class ProcessingError(Exception):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class TerminalProcessingError(ProcessingError):
    pass


class TransientProcessingError(ProcessingError):
    pass


class StageReporter:
    def __init__(
        self,
        *,
        repository: ProcessingJobRepository,
        job: ClaimedJob,
        lease_seconds: float,
    ) -> None:
        self.repository = repository
        self.job = job
        self.lease_seconds = lease_seconds

    async def stage(self, name: str) -> None:
        if name not in PROCESSING_STAGES:
            raise ValueError(f"unsupported processing stage: {name}")
        updated = await asyncio.to_thread(
            self.repository.report_stage,
            self.job,
            stage=name,
            now=_utc_now(),
            lease_seconds=self.lease_seconds,
        )
        if not updated:
            raise RuntimeError("processing job lease was lost")


class ProcessingWorker:
    """Single durable worker that processes one leased SQLite job at a time."""

    def __init__(
        self,
        *,
        repository: ProcessingJobRepository,
        processor: JobProcessor,
        worker_id: str | None = None,
        lease_seconds: float = 60,
        poll_seconds: float = 1,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.repository = repository
        self.processor = processor
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self._stop_event = asyncio.Event()

    async def run_once(self) -> bool:
        job = await asyncio.to_thread(
            self.repository.claim_next,
            worker_id=self.worker_id,
            now=_utc_now(),
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return False

        reporter = StageReporter(
            repository=self.repository,
            job=job,
            lease_seconds=self.lease_seconds,
        )
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            await self.processor.process(job, reporter)
        except TerminalProcessingError as error:
            await asyncio.to_thread(
                self.repository.fail,
                job,
                code=error.code,
                message=error.safe_message,
                retryable=False,
                now=_utc_now(),
            )
        except TransientProcessingError as error:
            await asyncio.to_thread(
                self.repository.fail,
                job,
                code=error.code,
                message=error.safe_message,
                retryable=True,
                now=_utc_now(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.to_thread(
                self.repository.fail,
                job,
                code="PROCESSING_TEMPORARY",
                message="媒体处理暂时失败",
                retryable=True,
                now=_utc_now(),
            )
        else:
            await asyncio.to_thread(self.repository.complete, job, now=_utc_now())
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def run_forever(self) -> None:
        self._stop_event.clear()
        while not self._stop_event.is_set():
            processed = await self.run_once()
            if processed:
                continue
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.poll_seconds,
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop_event.set()

    async def _heartbeat(self, job: ClaimedJob) -> None:
        interval = max(0.05, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            renewed = await asyncio.to_thread(
                self.repository.renew_lease,
                job,
                now=_utc_now(),
                lease_seconds=self.lease_seconds,
            )
            if not renewed:
                return


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
