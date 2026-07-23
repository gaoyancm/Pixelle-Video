"""Recoverable private-ComfyUI execution for durable media jobs."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from datetime import timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pixelle_video.services.comfyui_adapter import (
    ComfyUIAdapter,
    ComfyUIJob,
    ComfyUIJobState,
    ComfyUIOutput,
)
from pixelle_video.services.comfyui_workflows import get_workflow_spec

from .contracts import MediaOutputMetadata
from .models import MediaJob, utc_now
from .repository import MediaJobRepository
from .state_machine import ErrorCategory, JobStatus, RemoteJobStatus
from .worker import LeaseHandle, LeaseLostError

_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv"}
_MANAGED_COMFYUI_OUTPUT_TYPE = "output"
_PARAMETER_NAMES = {
    "prompt",
    "negative_prompt",
    "width",
    "height",
    "frame_count",
    "seed",
    "steps",
    "cfg",
    "output_prefix",
}


class ManagedAssetResolver:
    """Resolve persisted asset IDs strictly beneath a configured local root."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def resolve_first(self, job: MediaJob) -> Path:
        if not job.input_assets_json:
            raise ValueError(f"workflow '{job.workflow_type}' requires an input asset")
        asset_id = str(job.input_assets_json[0].get("asset_id") or "")
        posix = PurePosixPath(asset_id)
        if (
            not asset_id
            or "\\" in asset_id
            or posix.is_absolute()
            or PureWindowsPath(asset_id).is_absolute()
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise ValueError("input asset id is not a safe managed relative path")
        resolved = (self.root / Path(*posix.parts)).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise ValueError("input asset resolves outside the managed root") from None
        if not resolved.is_file():
            raise FileNotFoundError("managed input asset was not found")
        return resolved


class RecoverableComfyUIExecutor:
    """Perform one bounded submit, reconcile, or tracking step per lease claim."""

    def __init__(
        self,
        repository: MediaJobRepository,
        adapter: ComfyUIAdapter,
        *,
        managed_asset_root: str | Path,
        managed_output_root: str | Path,
        history_poll_interval_seconds: float = 2.0,
        clock=utc_now,
    ):
        self.repository = repository
        self.adapter = adapter
        self.assets = ManagedAssetResolver(managed_asset_root)
        self.output_root = Path(managed_output_root).resolve()
        self.history_poll_interval_seconds = history_poll_interval_seconds
        self._clock = clock

    async def process(self, job: MediaJob, lease: LeaseHandle) -> None:
        """Classify persisted state and execute one safe recovery step."""

        if self._deadline_reached(job):
            await self._finish(
                job,
                lease,
                JobStatus.TIMED_OUT,
                ErrorCategory.TIMEOUT,
                "persistent media job deadline reached",
            )
            return
        if job.cancel_requested_at is not None:
            await self._finish(
                job,
                lease,
                JobStatus.CANCELLED,
                ErrorCategory.CANCELLED,
                "platform cancellation requested; remote termination is not guaranteed",
            )
            return

        status = JobStatus(job.status)
        if status is JobStatus.QUEUED:
            await self._submit_new(job, lease)
            return
        if status is JobStatus.SUBMITTING:
            if job.comfyui_prompt_id:
                await self._track(job, lease, prompt_id=job.comfyui_prompt_id)
                return
            if job.submit_started_at is None:
                await lease.mutate(
                    lambda _status, version: self.repository.requeue_unsubmitted_job(
                        job.job_id,
                        expected_version=version,
                        lease_owner=lease.worker_id,
                    )
                )
                return
            await self._reconcile_unknown(job, lease)
            return
        if status is JobStatus.RUNNING:
            if not job.comfyui_prompt_id:
                await self._finish(
                    job,
                    lease,
                    JobStatus.FAILED,
                    ErrorCategory.INTERNAL,
                    "running media job has no persisted prompt id",
                )
                return
            await self._track(job, lease, prompt_id=job.comfyui_prompt_id)

    async def _submit_new(self, job: MediaJob, lease: LeaseHandle) -> None:
        try:
            parameters = self._build_parameters(job)
            prepared = await self.adapter.prepare_submission(
                job.workflow_type,
                **parameters,
            )
            if not job.node_id or prepared.node_id != job.node_id:
                raise ValueError("persisted node_id does not match selected ComfyUI node")
        except Exception as error:
            await self._finish(
                job,
                lease,
                JobStatus.FAILED,
                ErrorCategory.VALIDATION,
                f"private ComfyUI request preparation failed: {error}",
            )
            return

        await lease.mutate(
            lambda status, version: self.repository.transition_owned(
                job.job_id,
                expected_status=status,
                expected_version=version,
                lease_owner=lease.worker_id,
                target_status=JobStatus.SUBMITTING,
            )
        )
        await lease.mutate(
            lambda _status, version: self.repository.mark_submit_started_owned(
                job.job_id,
                expected_version=version,
                lease_owner=lease.worker_id,
                submit_started_at=self._clock(),
            )
        )
        try:
            remote_job = await self.adapter.submit_prepared(
                prepared,
                submission_token=job.submission_token,
            )
        except Exception as error:
            safe_message = f"ComfyUI submission result is unknown: {error}"
            await lease.mutate(
                lambda _status, version: self.repository.mark_submission_unknown_owned(
                    job.job_id,
                    expected_version=version,
                    lease_owner=lease.worker_id,
                    error_message=safe_message,
                )
            )
            await self._release_later(lease)
            return

        await lease.mutate(
            lambda _status, version: self.repository.record_prompt_owned(
                job.job_id,
                expected_version=version,
                lease_owner=lease.worker_id,
                prompt_id=remote_job.prompt_id,
            )
        )
        await self._track(job, lease, prompt_id=remote_job.prompt_id)

    async def _track(
        self,
        job: MediaJob,
        lease: LeaseHandle,
        *,
        prompt_id: str,
    ) -> None:
        if lease.status is JobStatus.SUBMITTING:
            await lease.mutate(
                lambda status, version: self.repository.transition_owned(
                    job.job_id,
                    expected_status=status,
                    expected_version=version,
                    lease_owner=lease.worker_id,
                    target_status=JobStatus.RUNNING,
                )
            )
        remote_job = ComfyUIJob(
            prompt_id=prompt_id,
            node_id=str(job.node_id),
            workflow_type=job.workflow_type,
        )
        try:
            status = await self.adapter.query_status(remote_job)
        except Exception:
            await self._release_later(lease)
            return

        if self._deadline_reached(job):
            await self._finish(
                job,
                lease,
                JobStatus.TIMED_OUT,
                ErrorCategory.TIMEOUT,
                "persistent media job deadline reached",
            )
            return
        if status.state is ComfyUIJobState.FAILED:
            await self._finish(
                job,
                lease,
                JobStatus.FAILED,
                ErrorCategory.REMOTE_FAILED,
                status.message or "ComfyUI execution failed",
            )
            return
        if status.state is ComfyUIJobState.COMPLETED:
            await self._complete(job, lease, remote_job)
            return

        remote_status = (
            RemoteJobStatus.RUNNING
            if status.state is ComfyUIJobState.RUNNING
            else RemoteJobStatus.QUEUED
        )
        await lease.mutate(
            lambda _status, version: self.repository.update_remote_status_owned(
                job.job_id,
                expected_version=version,
                lease_owner=lease.worker_id,
                remote_status=remote_status,
            )
        )
        await self._release_later(lease)

    async def _complete(
        self,
        job: MediaJob,
        lease: LeaseHandle,
        remote_job: ComfyUIJob,
    ) -> None:
        try:
            remote_outputs = await self.adapter.get_outputs(remote_job)
            outputs = await self._store_outputs(job, remote_outputs)
        except Exception as error:
            await self._finish(
                job,
                lease,
                JobStatus.FAILED,
                ErrorCategory.OUTPUT_MISSING,
                f"ComfyUI completed without a valid managed output: {error}",
            )
            return
        if not outputs:
            await self._finish(
                job,
                lease,
                JobStatus.FAILED,
                ErrorCategory.OUTPUT_MISSING,
                "ComfyUI completed without a valid media output",
            )
            return
        try:
            await lease.mutate(
                lambda _status, version: self.repository.write_outputs_owned(
                    job.job_id,
                    expected_version=version,
                    lease_owner=lease.worker_id,
                    outputs=outputs,
                )
            )
            await self._finish(
                job,
                lease,
                JobStatus.SUCCEEDED,
                None,
                None,
            )
        except LeaseLostError:
            await self._remove_outputs(outputs)
            raise

    async def _reconcile_unknown(self, job: MediaJob, lease: LeaseHandle) -> None:
        if job.error_category != ErrorCategory.SUBMISSION_UNKNOWN.value:
            await lease.mutate(
                lambda _status, version: self.repository.mark_submission_unknown_owned(
                    job.job_id,
                    expected_version=version,
                    lease_owner=lease.worker_id,
                    error_message="remote submission began but no prompt id was persisted",
                )
            )
        if not job.node_id:
            await self._release_later(lease)
            return
        try:
            matches = await self.adapter.find_prompt_ids_by_submission_token(
                job.node_id,
                job.submission_token,
            )
        except Exception:
            await self._release_later(lease)
            return
        if len(matches) != 1:
            await self._release_later(lease)
            return
        await lease.mutate(
            lambda _status, version: self.repository.record_prompt_owned(
                job.job_id,
                expected_version=version,
                lease_owner=lease.worker_id,
                prompt_id=matches[0],
            )
        )
        await self._release_later(lease)

    def _build_parameters(self, job: MediaJob) -> dict[str, Any]:
        get_workflow_spec(job.workflow_type)
        parameters = {
            key: value
            for key, value in job.input_json.items()
            if key in _PARAMETER_NAMES and value is not None
        }
        prompt = parameters.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("private ComfyUI media job requires a prompt")
        parameters.setdefault("output_prefix", f"media_jobs/{job.job_id}")
        spec = get_workflow_spec(job.workflow_type)
        if spec.requires_image:
            parameters["image_path"] = self.assets.resolve_first(job)
        return parameters

    async def _store_outputs(
        self,
        job: MediaJob,
        outputs: list[ComfyUIOutput],
    ) -> list[MediaOutputMetadata]:
        managed: list[MediaOutputMetadata] = []
        for index, output in enumerate(outputs):
            suffix = Path(output.filename).suffix.lower()
            if suffix not in _VIDEO_EXTENSIONS:
                continue
            if not self._is_safe_remote_output_reference(output):
                continue
            content = await self.adapter.download_output(output)
            relative = PurePosixPath(job.job_id, f"{index:03d}-{output.filename}")
            destination = self.output_root / Path(*relative.parts)
            await asyncio.to_thread(self._write_file, destination, content)
            managed.append(
                MediaOutputMetadata(
                    output_id=f"{job.job_id}-{index}",
                    media_type="video",
                    relative_path=relative.as_posix(),
                    size=len(content),
                    mime_type=mimetypes.guess_type(output.filename)[0]
                    or "application/octet-stream",
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
        return managed

    @staticmethod
    def _is_safe_remote_output_reference(output: ComfyUIOutput) -> bool:
        """Accept only a managed ComfyUI output reference before requesting /view."""

        filename = output.filename
        subfolder = output.subfolder
        if (
            not filename
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
            or output.storage_type != _MANAGED_COMFYUI_OUTPUT_TYPE
        ):
            return False
        if not subfolder:
            return True
        posix = PurePosixPath(subfolder)
        windows = PureWindowsPath(subfolder)
        raw_parts = subfolder.split("/")
        if (
            "\\" in subfolder
            or posix.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or any(part in {"", ".", ".."} for part in raw_parts)
        ):
            return False
        return True

    @staticmethod
    def _write_file(destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(destination)

    async def _remove_outputs(self, outputs: list[MediaOutputMetadata]) -> None:
        for output in outputs:
            path = self.output_root / Path(*PurePosixPath(output.relative_path).parts)
            await asyncio.to_thread(path.unlink, missing_ok=True)

    async def _release_later(self, lease: LeaseHandle) -> None:
        next_attempt = self._clock() + timedelta(
            seconds=self.history_poll_interval_seconds
        )
        await lease.mutate(
            lambda status, version: self.repository.release_owned(
                lease.job_id,
                expected_status=status,
                expected_version=version,
                lease_owner=lease.worker_id,
                next_attempt_at=next_attempt,
            )
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
            lambda status, version: self.repository.transition_owned(
                job.job_id,
                expected_status=status,
                expected_version=version,
                lease_owner=lease.worker_id,
                target_status=target,
                error_category=category,
                error_message=message,
            )
        )

    def _deadline_reached(self, job: MediaJob) -> bool:
        deadline = job.deadline_at
        return deadline is not None and deadline <= self._clock()
