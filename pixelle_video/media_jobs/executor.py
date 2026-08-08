"""Recoverable private-ComfyUI execution for durable media jobs."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
from datetime import timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Awaitable, Callable

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

if TYPE_CHECKING:
    from pixelle_video.media_assets import AssetService

_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv"}
_MANAGED_COMFYUI_OUTPUT_TYPE = "output"
_LEGACY_ASSET_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_UUID_SHAPED = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
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
        windows = PureWindowsPath(asset_id)
        if (
            not asset_id
            or not _LEGACY_ASSET_ID.fullmatch(asset_id)
            or _UUID_SHAPED.fullmatch(asset_id)
            or "\\" in asset_id
            or posix.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
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
        asset_service: AssetService | None = None,
        output_validator: Callable[[str], Awaitable[Any]] | None = None,
        clock=utc_now,
    ):
        self.repository = repository
        self.adapter = adapter
        self.assets = ManagedAssetResolver(managed_asset_root)
        self.asset_service = asset_service
        self.output_root = Path(managed_output_root).resolve()
        self.history_poll_interval_seconds = history_poll_interval_seconds
        self.output_validator = output_validator
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
            parameters = await self._build_parameters(job)
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
        validation_message = await self._validate_outputs_diagnostic(job)
        if (
            self.asset_service is not None
            and await self.asset_service.repository.has_output_relations(job.job_id)
        ):
            if await self.asset_service.validate_committed_output_group(job.job_id):
                await self._finish(job, lease, JobStatus.SUCCEEDED, None, validation_message)
            else:
                await self._finish(
                    job,
                    lease,
                    JobStatus.FAILED,
                    ErrorCategory.OUTPUT_MISSING,
                    "persisted output assets are incomplete or unavailable",
                )
            return
        try:
            remote_outputs = await self.adapter.get_outputs(remote_job)
            outputs, generated_assets = await self._store_outputs(job, remote_outputs)
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
        registered = False
        try:
            if self.asset_service is not None:
                await lease.mutate(
                    lambda _status, version: self.asset_service.repository.register_output_group(
                        job_id=job.job_id,
                        lease_owner=lease.worker_id,
                        expected_version=version,
                        assets=generated_assets,
                        roles=["generated_video"] * len(generated_assets),
                        output_metadata=[output.model_dump() for output in outputs],
                    )
                )
                registered = True
            else:
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
                validation_message,
            )
        except LeaseLostError:
            if not registered:
                await self._remove_outputs(outputs, generated_assets)
            raise
        except Exception as error:
            if self.asset_service is None:
                await self._remove_outputs(outputs, generated_assets)
                raise
            from pixelle_video.media_assets.repository import (
                OutputRegistrationDisposition,
                OutputRegistrationError,
            )

            if not isinstance(error, OutputRegistrationError):
                raise
            if error.disposition is OutputRegistrationDisposition.NOT_COMMITTED:
                await self._remove_outputs(outputs, generated_assets)
                await self._finish(
                    job,
                    lease,
                    JobStatus.FAILED,
                    ErrorCategory.INTERNAL,
                    "output asset registration failed before commit",
                )
                return
            await self._release_later(lease)

    async def _validate_outputs_diagnostic(self, job: MediaJob) -> str | None:
        """Run the optional F3 output contract validator; never blocks completion."""
        if self.output_validator is None:
            return None
        try:
            result = await self.output_validator(job.job_id)
        except Exception:
            return None
        issues = getattr(result, "issues", ())
        if not issues:
            return None
        critical = [issue for issue in issues if getattr(issue, "severity", "") == "critical"]
        count = len(critical) or len(issues)
        return f"output contract validation found {count} issue(s)"

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

    async def _build_parameters(self, job: MediaJob) -> dict[str, Any]:
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
            if self.asset_service is not None:
                managed = await self.asset_service.resolve_job_input(job.job_id)
                if managed is not None:
                    parameters["image_path"] = managed
                    return parameters
            # 02-E removal target: historical 02-A through 02-C path references only.
            parameters["image_path"] = self.assets.resolve_first(job)
        return parameters

    async def _store_outputs(
        self,
        job: MediaJob,
        outputs: list[ComfyUIOutput],
    ) -> tuple[list[MediaOutputMetadata], list]:
        managed: list[MediaOutputMetadata] = []
        generated_assets = []
        for index, output in enumerate(outputs):
            suffix = Path(output.filename).suffix.lower()
            if suffix not in _VIDEO_EXTENSIONS:
                continue
            if not self._is_safe_remote_output_reference(output):
                continue
            try:
                content = await self.adapter.download_output(output)
                if self.asset_service is not None:
                    asset = await self.asset_service.register_generated_bytes(
                        content, filename=output.filename
                    )
                    generated_assets.append(asset)
                    managed.append(
                        MediaOutputMetadata(
                            output_id=asset.id,
                            media_type=asset.media_type,
                            relative_path=asset.object_key,
                            size=asset.size_bytes,
                            mime_type=asset.mime_type,
                            sha256=asset.sha256,
                        )
                    )
                    continue
            except Exception:
                if self.asset_service is not None:
                    for asset in generated_assets:
                        self.asset_service.discard_unregistered(asset)
                raise
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
        return managed, generated_assets

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

    async def _remove_outputs(self, outputs: list[MediaOutputMetadata], generated_assets=None) -> None:
        if self.asset_service is not None:
            for asset in generated_assets or []:
                self.asset_service.discard_unregistered(asset)
            return
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
