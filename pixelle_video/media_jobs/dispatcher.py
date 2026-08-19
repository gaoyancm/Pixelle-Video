"""Executor registry and dispatcher for the persistent media-job worker.

The phase-02 worker originally wired a single ``RecoverableComfyUIExecutor``
directly.  Product lines later started emitting ``llm_caption`` and legacy
``comfyui`` jobs that no single ComfyUI executor could consume, leaving those
jobs permanently queued.  This module introduces:

- ``LLMCaptionJobProcessor``: consumes ``executor_kind="llm_caption"`` jobs by
  calling an injectable text generator and persisting the caption as a JSON
  output bound to the media job.
- ``DispatchingJobProcessor``: routes each job by its persisted ``executor_kind``
  and turns unknown kinds into a diagnosable terminal state instead of crashing
  the worker loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath

from .contracts import MediaOutputMetadata
from .executor import RecoverableComfyUIExecutor
from .models import MediaJob, utc_now
from .repository import MediaJobRepository
from .state_machine import ErrorCategory, JobStatus
from .worker import JobProcessor, LeaseHandle

TextGenerator = Callable[[str], Awaitable[str]]


class LLMCaptionJobProcessor:
    """Persistently execute ``llm_caption`` jobs through an injectable LLM call.

    The caption result is written as a UTF-8 JSON file beneath the managed
    output root and registered via ``write_outputs_owned`` so downstream
    delivery packaging can read a real, queryable output rather than a template.
    """

    def __init__(
        self,
        repository: MediaJobRepository,
        *,
        llm_caller: TextGenerator,
        managed_output_root: str | Path,
        caption_timeout_seconds: float = 60.0,
        clock=utc_now,
    ):
        self.repository = repository
        self.llm_caller = llm_caller
        self.output_root = Path(managed_output_root).resolve()
        self.caption_timeout_seconds = caption_timeout_seconds
        self._clock = clock

    async def process(self, job: MediaJob, lease: LeaseHandle) -> None:
        if self._deadline_reached(job):
            await self._finish(
                job,
                lease,
                JobStatus.TIMED_OUT,
                ErrorCategory.TIMEOUT,
                "caption media job deadline reached",
            )
            return
        if job.cancel_requested_at is not None:
            await self._finish(
                job,
                lease,
                JobStatus.CANCELLED,
                ErrorCategory.CANCELLED,
                "platform cancellation requested",
            )
            return

        status = JobStatus(job.status)
        if status is JobStatus.RUNNING:
            await self._recover_running(job, lease)
            return
        if status not in {JobStatus.QUEUED, JobStatus.SUBMITTING}:
            return

        if status is JobStatus.QUEUED:
            await lease.mutate(
                lambda s, v: self.repository.transition_owned(
                    job.job_id,
                    expected_status=s,
                    expected_version=v,
                    lease_owner=lease.worker_id,
                    target_status=JobStatus.SUBMITTING,
                )
            )
        await lease.mutate(
            lambda s, v: self.repository.transition_owned(
                job.job_id,
                expected_status=s,
                expected_version=v,
                lease_owner=lease.worker_id,
                target_status=JobStatus.RUNNING,
            )
        )
        await self._generate_and_finish(job, lease)

    async def _recover_running(self, job: MediaJob, lease: LeaseHandle) -> None:
        """Close a reclaimed caption without repeating an uncertain LLM call.

        A crash can happen after the external call, after the atomic file write,
        or after the database output write.  Existing durable output is completed;
        an orphaned managed file is re-registered; otherwise the uncertain call is
        failed explicitly so it can never be billed twice automatically.
        """

        if job.output_metadata:
            await self._finish(job, lease, JobStatus.SUCCEEDED, None, None)
            return

        relative = PurePosixPath(job.job_id, "000-caption.json")
        destination = self.output_root / Path(*relative.parts)
        if destination.is_file():
            try:
                raw = destination.read_bytes()
                text = str(json.loads(raw.decode("utf-8")).get("text") or "").strip()
                if not text:
                    raise ValueError("caption text is empty")
                output = MediaOutputMetadata(
                    output_id=f"{job.job_id}-caption",
                    media_type="text",
                    relative_path=relative.as_posix(),
                    size=len(raw),
                    mime_type="application/json",
                    sha256=hashlib.sha256(raw).hexdigest(),
                    content=text,
                )
                await lease.mutate(
                    lambda _s, v: self.repository.write_outputs_owned(
                        job.job_id,
                        expected_version=v,
                        lease_owner=lease.worker_id,
                        outputs=[output],
                    )
                )
                await self._finish(job, lease, JobStatus.SUCCEEDED, None, None)
                return
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                pass

        await self._finish(
            job,
            lease,
            JobStatus.FAILED,
            ErrorCategory.SUBMISSION_UNKNOWN,
            "caption generation outcome is unknown after worker recovery",
        )

    async def _generate_and_finish(self, job: MediaJob, lease: LeaseHandle) -> None:
        prompt = str(
            job.input_json.get("prompt_hint") or job.input_json.get("prompt") or ""
        ).strip()
        if not prompt:
            await self._finish(
                job,
                lease,
                JobStatus.FAILED,
                ErrorCategory.VALIDATION,
                "llm_caption media job requires a prompt",
            )
            return

        try:
            text = await asyncio.wait_for(
                self.llm_caller(prompt), timeout=self.caption_timeout_seconds
            )
        except asyncio.TimeoutError:
            await self._finish(
                job,
                lease,
                JobStatus.FAILED,
                ErrorCategory.TIMEOUT,
                "caption generation timed out",
            )
            return
        except Exception as error:
            await self._finish(
                job,
                lease,
                JobStatus.FAILED,
                ErrorCategory.INTERNAL,
                f"caption generation failed: {type(error).__name__}",
            )
            return

        if not isinstance(text, str) or not text.strip():
            await self._finish(
                job,
                lease,
                JobStatus.FAILED,
                ErrorCategory.OUTPUT_MISSING,
                "caption generation produced empty text",
            )
            return

        try:
            output = self._write_caption_text(job, text)
            await lease.mutate(
                lambda _s, v: self.repository.write_outputs_owned(
                    job.job_id,
                    expected_version=v,
                    lease_owner=lease.worker_id,
                    outputs=[output],
                )
            )
        except Exception as error:
            await self._finish(
                job,
                lease,
                JobStatus.FAILED,
                ErrorCategory.STORAGE_FAILED,
                f"caption output storage failed: {type(error).__name__}",
            )
            return

        await self._finish(job, lease, JobStatus.SUCCEEDED, None, None)

    def _write_caption_text(self, job: MediaJob, text: str) -> MediaOutputMetadata:
        relative = PurePosixPath(job.job_id, "000-caption.json")
        destination = self.output_root / Path(*relative.parts)
        content = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(destination)
        return MediaOutputMetadata(
            output_id=f"{job.job_id}-caption",
            media_type="text",
            relative_path=relative.as_posix(),
            size=len(content),
            mime_type="application/json",
            sha256=hashlib.sha256(content).hexdigest(),
            content=text,
        )

    async def _finish(
        self,
        job: MediaJob,
        lease: LeaseHandle,
        target: JobStatus,
        category: ErrorCategory | None,
        message: str | None,
    ) -> None:
        await lease.mutate(
            lambda s, v: self.repository.transition_owned(
                job.job_id,
                expected_status=s,
                expected_version=v,
                lease_owner=lease.worker_id,
                target_status=target,
                error_category=category,
                error_message=message,
            )
        )

    def _deadline_reached(self, job: MediaJob) -> bool:
        deadline = job.deadline_at
        return deadline is not None and deadline <= self._clock()


class DispatchingJobProcessor:
    """Route each job to its registered processor by persisted ``executor_kind``.

    An unknown ``executor_kind`` is a configuration error: the job is moved to a
    diagnosable ``failed`` terminal state without raising, so a single malformed
    row can never kill the worker loop.
    """

    def __init__(
        self,
        repository: MediaJobRepository,
        processors: dict[str, JobProcessor],
        *,
        clock=utc_now,
    ):
        self.repository = repository
        self._processors = dict(processors)
        self._clock = clock

    def processor_kinds(self) -> list[str]:
        """Return the registered executor kinds (for readiness reporting)."""

        return sorted(self._processors)

    async def process(self, job: MediaJob, lease: LeaseHandle) -> None:
        processor = self._processors.get(job.executor_kind)
        if processor is None:
            await lease.mutate(
                lambda s, v: self.repository.transition_owned(
                    job.job_id,
                    expected_status=s,
                    expected_version=v,
                    lease_owner=lease.worker_id,
                    target_status=JobStatus.FAILED,
                    error_category=ErrorCategory.CONFIGURATION,
                    error_message=(
                        f"no processor registered for executor_kind '{job.executor_kind}'"
                    ),
                )
            )
            return
        await processor.process(job, lease)


def build_default_processors(
    repository: MediaJobRepository,
    *,
    comfyui_executor: RecoverableComfyUIExecutor,
    llm_caller: TextGenerator,
    managed_output_root: str | Path,
    caption_timeout_seconds: float = 60.0,
) -> dict[str, JobProcessor]:
    """Assemble the production processor registry used by ``worker_cli``."""

    return {
        "private_comfyui": comfyui_executor,
        "llm_caption": LLMCaptionJobProcessor(
            repository,
            llm_caller=llm_caller,
            managed_output_root=managed_output_root,
            caption_timeout_seconds=caption_timeout_seconds,
        ),
    }
