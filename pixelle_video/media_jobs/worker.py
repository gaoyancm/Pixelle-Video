"""Independent database-polling worker and durable lease lifecycle."""

from __future__ import annotations

import asyncio
import socket
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Protocol, TypeVar

from loguru import logger

from .models import MediaJob, utc_now
from .repository import CASConflictError, MediaJobRepository
from .state_machine import JobStatus

_T = TypeVar("_T")


class LeaseLostError(RuntimeError):
    """The worker no longer owns the durable write lease for a job."""


class JobProcessor(Protocol):
    async def process(self, job: MediaJob, lease: "LeaseHandle") -> None:
        """Perform one bounded, recoverable execution step."""


class LeaseHandle:
    """Serialize local CAS writes with heartbeat version changes."""

    def __init__(
        self,
        repository: MediaJobRepository,
        job: MediaJob,
        *,
        worker_id: str,
        lease_seconds: float,
        clock: Callable[[], datetime] = utc_now,
    ):
        self._repository = repository
        self.job_id = job.job_id
        self.worker_id = worker_id
        self.status = JobStatus(job.status)
        self.version = job.version
        self.lease_seconds = lease_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._lost = asyncio.Event()

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def mark_lost(self) -> None:
        self._lost.set()

    async def mutate(
        self,
        operation: Callable[[JobStatus, int], Awaitable[MediaJob]],
    ) -> MediaJob:
        """Run one owned CAS mutation and synchronize local status/version."""

        async with self._lock:
            if self.lost:
                raise LeaseLostError(f"worker lease lost for job {self.job_id}")
            try:
                job = await operation(self.status, self.version)
            except CASConflictError as error:
                self.mark_lost()
                raise LeaseLostError(f"worker lease lost for job {self.job_id}") from error
            self.status = JobStatus(job.status)
            self.version = job.version
            return job

    async def heartbeat(self) -> MediaJob:
        """Extend the lease while preserving the shared CAS version."""

        expires_at = self._clock() + timedelta(seconds=self.lease_seconds)
        return await self.mutate(
            lambda status, version: self._repository.update_heartbeat(
                self.job_id,
                expected_status=status,
                expected_version=version,
                lease_owner=self.worker_id,
                lease_expires_at=expires_at,
            )
        )


def default_worker_id() -> str:
    """Return a process-unique, diagnosable worker identifier."""

    return f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"


class MediaJobWorker:
    """Poll durable jobs and process them without holding database transactions."""

    def __init__(
        self,
        repository: MediaJobRepository,
        processor: JobProcessor,
        *,
        worker_id: str | None = None,
        poll_interval_seconds: float = 2.0,
        lease_seconds: float = 60.0,
        heartbeat_seconds: float = 20.0,
        recovery_scan_interval_seconds: float = 10.0,
        candidate_limit: int = 20,
        clock: Callable[[], datetime] = utc_now,
    ):
        if heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds must be positive and shorter than lease_seconds")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if recovery_scan_interval_seconds <= 0:
            raise ValueError("recovery_scan_interval_seconds must be positive")
        self.repository = repository
        self.processor = processor
        self.worker_id = worker_id or default_worker_id()
        self.poll_interval_seconds = poll_interval_seconds
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.recovery_scan_interval_seconds = recovery_scan_interval_seconds
        self.candidate_limit = candidate_limit
        self._clock = clock
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        """Stop claiming new work after the current bounded step."""

        self._stop.set()

    async def run_once(self, *, include_recovery: bool = True) -> int:
        """Claim and process currently eligible jobs once."""

        if self._stop.is_set():
            return 0
        candidates = []
        if include_recovery:
            candidates.extend(
                await self.repository.list_claim_candidates(
                    now=self._clock(),
                    limit=self.candidate_limit,
                    statuses=(JobStatus.SUBMITTING, JobStatus.RUNNING),
                )
            )
        remaining = self.candidate_limit - len(candidates)
        if remaining:
            candidates.extend(
                await self.repository.list_claim_candidates(
                    now=self._clock(),
                    limit=remaining,
                    statuses=(JobStatus.QUEUED,),
                )
            )
        processed = 0
        for candidate in candidates:
            if self._stop.is_set():
                break
            try:
                claimed = await self.repository.claim_lease(
                    candidate.job_id,
                    expected_status=JobStatus(candidate.status),
                    expected_version=candidate.version,
                    lease_owner=self.worker_id,
                    lease_expires_at=self._clock() + timedelta(seconds=self.lease_seconds),
                )
            except CASConflictError:
                continue
            logger.info(
                "Media worker claimed job_id={} worker_id={} status={}",
                candidate.job_id,
                self.worker_id,
                candidate.status,
            )

            lease = LeaseHandle(
                self.repository,
                claimed,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
                clock=self._clock,
            )
            heartbeat_stop = asyncio.Event()
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(lease, heartbeat_stop),
                name=f"media-job-heartbeat-{candidate.job_id}",
            )
            try:
                await self.processor.process(claimed, lease)
            except LeaseLostError:
                logger.warning(
                    "Media worker lost lease job_id={} worker_id={} status={}",
                    candidate.job_id,
                    self.worker_id,
                    lease.status.value,
                )
            finally:
                heartbeat_stop.set()
                await heartbeat_task
            processed += 1
        return processed

    async def run_forever(self) -> None:
        """Run until a graceful stop is requested."""

        loop = asyncio.get_running_loop()
        next_recovery_scan = 0.0
        while not self._stop.is_set():
            now = loop.time()
            include_recovery = now >= next_recovery_scan
            await self.run_once(include_recovery=include_recovery)
            if include_recovery:
                next_recovery_scan = now + self.recovery_scan_interval_seconds
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.poll_interval_seconds,
                )
            except TimeoutError:
                continue

    async def _heartbeat_loop(
        self,
        lease: LeaseHandle,
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set() and not lease.lost:
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_seconds)
                return
            except TimeoutError:
                pass
            try:
                await lease.heartbeat()
            except LeaseLostError:
                return
