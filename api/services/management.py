"""Application service for phase 03-B management and atomic batch submission."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Sequence

from pydantic import ValidationError

from api.schemas.media_jobs import MediaJobRequest
from pixelle_video.config.schema import MediaJobsConfig
from pixelle_video.management import (
    BatchSubmission,
    DraftAssetReference,
    DraftItem,
    ManagementConflictError,
    ManagementRepository,
    Priority,
    SubmissionItem,
    effective_priority,
    freeze_generation_parameters,
    normalize_priority,
)
from pixelle_video.media_assets import AssetNotFoundError, AssetService
from pixelle_video.media_assets.contracts import AssetKind, AssetState
from pixelle_video.media_jobs.contracts import MediaInputAsset, MediaJobCreate, reject_secret_fields
from pixelle_video.media_jobs.models import utc_now
from pixelle_video.media_jobs.state_machine import ErrorCategory, JobStatus, can_retry, is_terminal
from pixelle_video.services.comfyui_workflows import WORKFLOW_SPECS


class PreflightFailedError(RuntimeError):
    def __init__(self, issues: list[dict[str, Any]]):
        super().__init__("batch preflight failed")
        self.issues = issues


@dataclass(frozen=True)
class PreparedItem:
    item_id: str
    position: int
    overrides: dict[str, Any]
    effective_parameters: dict[str, Any]
    priority_override: int | None
    priority: int
    assets: tuple[DraftAssetReference, ...]


@dataclass(frozen=True)
class PreparedBatch:
    batch: Any
    items: tuple[PreparedItem, ...]
    issues: tuple[dict[str, Any], ...]
    node_id: str | None


_PRIORITY_NAMES = {0: "low", 1: "normal", 2: "high"}
_FORBIDDEN_DRAFT_KEYS = {
    "priority",
    "priority_override",
    "node_id",
    "workflow_key",
    "provider",
    "executor_kind",
    "base_url",
}


class ManagementApplicationService:
    def __init__(
        self,
        repository: ManagementRepository,
        config: MediaJobsConfig,
        assets: AssetService,
        *,
        node_selector: Callable[[str], str],
        configured_nodes: Sequence[Any] = (),
    ):
        self.repository = repository
        self.config = config
        self.assets = assets
        self.node_selector = node_selector
        self.configured_nodes = tuple(configured_nodes)

    @staticmethod
    def priority_name(value: int | None) -> str | None:
        return None if value is None else _PRIORITY_NAMES[int(value)]

    async def create_project(self, *, name: str, description: str | None):
        return await self.repository.create_project(name=name, description=description)

    async def list_projects(self, *, include_archived: bool, limit: int, offset: int):
        rows = await self.repository.list_projects(
            include_archived=include_archived, limit=limit + 1, offset=offset
        )
        return rows[:limit], len(rows) > limit

    async def get_project(self, project_id: str):
        return await self.repository.get_project(project_id)

    async def update_project(self, project_id: str, *, name: str, description: str | None):
        return await self.repository.update_project(project_id, name=name, description=description)

    async def archive_project(self, project_id: str):
        return await self.repository.archive_project(project_id)

    async def create_batch(self, project_id: str, **values):
        project = await self.repository.get_project(project_id)
        if project.archived_at is not None:
            raise ManagementConflictError("archived project cannot accept new batches")
        self._require_workflow(values["workflow_type"])
        self._validate_draft_parameters(values["common_parameters"])
        return await self.repository.create_batch(project_id=project_id, **values)

    async def list_batches(
        self, project_id: str, *, include_archived: bool, limit: int, offset: int
    ):
        await self.repository.get_project(project_id)
        rows = await self.repository.list_batches(
            project_id=project_id,
            include_archived=include_archived,
            limit=limit + 1,
            offset=offset,
        )
        return rows[:limit], len(rows) > limit

    async def get_batch(self, batch_id: str):
        return await self.repository.get_batch(batch_id)

    async def update_batch(self, batch_id: str, **values):
        self._require_workflow(values["workflow_type"])
        self._validate_draft_parameters(values["common_parameters"])
        return await self.repository.update_draft_batch(batch_id, **values)

    async def archive_batch(self, batch_id: str):
        return await self.repository.archive_batch(batch_id)

    async def replace_items(self, batch_id: str, *, expected_version: int, items: Sequence[Any]):
        drafts: list[DraftItem] = []
        for item in items:
            self._validate_draft_parameters(item.parameter_overrides)
            assets = tuple(
                DraftAssetReference(asset.asset_id, asset.role, asset.position)
                for asset in item.assets
            )
            for asset in assets:
                await self._readable_input_asset(asset.asset_id)
            drafts.append(
                DraftItem(
                    item_id=item.item_id,
                    position=item.position,
                    parameter_overrides=item.parameter_overrides,
                    priority_override=(
                        None
                        if item.priority_override is None
                        else int(normalize_priority(item.priority_override))
                    ),
                    assets=assets,
                )
            )
        written, version = await self.repository.replace_draft_items(
            batch_id, drafts, expected_version=expected_version
        )
        return written, version

    async def item_payloads(self, batch_id: str) -> list[dict[str, Any]]:
        rows = await self.repository.list_items(batch_id)
        payloads = []
        for row in rows:
            assets = await self.repository.list_item_assets(row.id)
            payloads.append(
                {
                    "item_id": row.id,
                    "position": row.position,
                    "parameter_overrides": row.parameter_overrides_json,
                    "priority_override": self.priority_name(row.priority_override),
                    "assets": [
                        {"asset_id": item.asset_id, "role": item.role, "position": item.position}
                        for item in assets
                    ],
                }
            )
        return payloads

    def workflow_catalog(self) -> list[dict[str, Any]]:
        items = []
        parameter_schema = MediaJobRequest.model_fields["parameters"].annotation.model_json_schema()
        public_properties = parameter_schema["properties"]
        for workflow, spec in sorted(WORKFLOW_SPECS.items()):
            available = (
                any(
                    getattr(node, "enabled", False)
                    and workflow in getattr(node, "workflow_types", ())
                    for node in self.configured_nodes
                )
                and self.config.enabled
                and self.config.private_comfyui_enabled
            )
            fields = MediaJobRequest.model_fields["parameters"].annotation.model_fields
            parameters = []
            for name in spec.parameter_targets:
                if name in {"input_image", "output_prefix"} or name not in fields:
                    continue
                field = fields[name]
                property_schema = public_properties[name]
                constraints = {
                    key: value
                    for candidate in [property_schema, *property_schema.get("anyOf", [])]
                    for key, value in candidate.items()
                    if key
                    in {
                        "minimum",
                        "maximum",
                        "minLength",
                        "maxLength",
                        "default",
                    }
                }
                parameters.append(
                    {
                        "name": name,
                        "required": field.is_required(),
                        "type": str(field.annotation).replace("typing.", ""),
                        "constraints": constraints,
                    }
                )
            items.append(
                {
                    "workflow": workflow,
                    "display_name": workflow.replace("_", " ").upper(),
                    "mode": "image_to_video" if spec.requires_image else "text_to_video",
                    "requires_image": spec.requires_image,
                    "input_asset_count": 1 if spec.requires_image else 0,
                    "available": available,
                    "unavailable_reason": None if available else "no_enabled_node",
                    "parameters": parameters,
                }
            )
        return items

    async def preflight(self, batch_id: str, *, expected_version: int) -> PreparedBatch:
        prepared = await self._prepare(
            batch_id, expected_version=expected_version, select_node=True
        )
        return prepared

    async def submit(
        self, batch_id: str, *, expected_version: int, idempotency_key: str
    ) -> tuple[dict[str, Any], bool]:
        operation = await self.repository.get_operation(
            scope_type="batch",
            scope_id=batch_id,
            operation_type="batch_submit",
            idempotency_key=idempotency_key,
        )
        # Reconstruct stable content without requiring the batch to remain a draft.
        replay_snapshot = await self._prepare(
            batch_id, expected_version=expected_version, select_node=False, require_draft=False
        )
        request_hash = self._canonical_hash(replay_snapshot, expected_version)
        if operation is not None:
            if operation.request_hash != request_hash:
                raise ManagementConflictError(
                    "management operation key belongs to a different request"
                )
            if operation.result_json is None:
                from pixelle_video.management import SubmissionIndeterminateError

                raise SubmissionIndeterminateError("batch submission result is incomplete")
            await self.repository.validate_submission_result(batch_id, operation.result_json)
            return operation.result_json, False

        prepared = await self._prepare(
            batch_id, expected_version=expected_version, select_node=True
        )
        if prepared.issues:
            raise PreflightFailedError(list(prepared.issues))
        operation_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"phase3-batch-submit:{batch_id}:{idempotency_key}:{request_hash}",
            )
        )
        jobs = []
        planned = []
        spec = WORKFLOW_SPECS[prepared.batch.workflow_type]
        for item in prepared.items:
            job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{operation_id}:{item.item_id}"))
            scoped_key = hashlib.sha256(
                f"batch:{batch_id}:item:{item.item_id}:{idempotency_key}".encode("utf-8")
            ).hexdigest()
            input_assets = [
                MediaInputAsset(asset_id=asset.asset_id, role=asset.role) for asset in item.assets
            ]
            create = MediaJobCreate(
                job_id=job_id,
                workflow_type=prepared.batch.workflow_type,
                workflow_key=spec.workflow_key,
                executor_kind="private_comfyui",
                provider="private_comfyui",
                node_id=prepared.node_id,
                input_json=item.effective_parameters,
                input_assets_json=input_assets,
                idempotency_key=scoped_key,
                deadline_at=utc_now() + timedelta(seconds=self.config.default_timeout_seconds),
                priority=item.priority,
            )
            jobs.append(
                {
                    "item_id": item.item_id,
                    "job_id": job_id,
                    "status": "queued",
                    "priority": self.priority_name(item.priority),
                }
            )
            planned.append(
                SubmissionItem(
                    item_id=item.item_id,
                    effective_parameters=item.effective_parameters,
                    priority=item.priority,
                    create=create,
                )
            )
        result_json = {
            "batch_id": batch_id,
            "batch_version": expected_version + 1,
            "operation_id": operation_id,
            "jobs": jobs,
        }
        result = await self.repository.submit_batch(
            BatchSubmission(
                batch_id=batch_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                operation_id=operation_id,
                items=tuple(planned),
                result_json=result_json,
            )
        )
        return result.result_json, result.created

    async def update_batch_priority(self, batch_id: str, *, expected_version: int, priority: str):
        result = await self.repository.update_batch_priority(
            batch_id, expected_version=expected_version, priority=int(normalize_priority(priority))
        )
        result["default_priority"] = self.priority_name(result["default_priority"])
        return result

    async def update_item_priority(
        self, item_id: str, *, expected_version: int, priority: str | None
    ):
        result = await self.repository.update_item_priority(
            item_id,
            expected_version=expected_version,
            priority=None if priority is None else int(normalize_priority(priority)),
        )
        result["priority_override"] = self.priority_name(result["priority_override"])
        result["effective_priority"] = self.priority_name(result["effective_priority"])
        return result

    async def cancel_batch(self, batch_id: str, *, expected_version: int, idempotency_key: str):
        request_hash = self._operation_hash(batch_id, expected_version)
        operation_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"phase3-batch-cancel:{batch_id}:{idempotency_key}:{request_hash}",
            )
        )
        return await self.repository.cancel_batch(
            batch_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            operation_id=operation_id,
        )

    async def retry_eligible(self, batch_id: str, *, expected_version: int, idempotency_key: str):
        request_hash = self._operation_hash(batch_id, expected_version)
        operation_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"phase3-retry-eligible:{batch_id}:{idempotency_key}:{request_hash}",
            )
        )
        return await self.repository.retry_eligible(
            batch_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            operation_id=operation_id,
            deadline_at=utc_now() + timedelta(seconds=self.config.default_timeout_seconds),
        )

    async def progress(self, batch_id: str) -> dict[str, Any]:
        snapshot = await self.repository.execution_snapshot(batch_id)
        current = self._current_jobs(snapshot)
        counts = {
            name: 0
            for name in (
                "queued",
                "submitting",
                "running",
                "cancel_requested",
                "succeeded",
                "failed",
                "timed_out",
                "cancelled",
            )
        }
        for job in current.values():
            status = JobStatus(job.status)
            if is_terminal(status):
                counts[status.value] += 1
            elif job.cancel_requested_at is not None:
                counts["cancel_requested"] += 1
            else:
                counts[status.value] += 1
        total = len(snapshot["items"])
        completed = sum(counts[name] for name in ("succeeded", "failed", "timed_out", "cancelled"))
        result = None
        if completed == total:
            if counts["succeeded"] == total:
                result = "all_succeeded"
            elif counts["succeeded"]:
                result = "partially_succeeded"
            elif counts["cancelled"]:
                result = "completed_with_cancellation"
            else:
                result = "all_failed"
        return {
            "batch_id": batch_id,
            "batch_state": snapshot["batch"].state,
            "total_items": total,
            "completed_items": completed,
            "derived_result": result,
            "counts": counts,
        }

    async def results(self, batch_id: str) -> dict[str, Any]:
        snapshot = await self.repository.execution_snapshot(batch_id)
        jobs = {job.job_id: job for job in snapshot["jobs"]}
        attempts_by_item: dict[str, list[Any]] = {}
        for attempt in snapshot["attempts"]:
            attempts_by_item.setdefault(attempt.item_id, []).append(attempt)
        outputs: dict[str, list[tuple[Any, Any]]] = {}
        for relation, asset in snapshot["outputs"]:
            outputs.setdefault(relation.job_id, []).append((relation, asset))
        items = []
        for item in snapshot["items"]:
            histories = []
            attempts = attempts_by_item.get(item.id, [])
            for attempt in attempts:
                job = jobs.get(attempt.media_job_id)
                if job is None:
                    continue
                error = None
                category = None
                if job.error_category:
                    error = {
                        "code": job.error_category,
                        "message": "The media job did not complete successfully.",
                    }
                    try:
                        category = ErrorCategory(job.error_category)
                    except ValueError:
                        category = None
                metadata = {entry.get("output_id"): entry for entry in job.output_metadata}
                assets = []
                for _relation, asset in outputs.get(job.job_id, []):
                    projection = metadata.get(asset.id, {})
                    href = f"/api/assets/{asset.id}/content"
                    assets.append(
                        {
                            "asset_id": asset.id,
                            "original_filename": asset.original_filename,
                            "media_type": asset.media_type,
                            "mime_type": asset.mime_type,
                            "size_bytes": asset.size_bytes,
                            "width": projection.get("width"),
                            "height": projection.get("height"),
                            "duration": projection.get("duration"),
                            "content_href": href,
                            "preview_href": href,
                        }
                    )
                histories.append(
                    {
                        "attempt_no": attempt.attempt_no,
                        "job_id": job.job_id,
                        "workflow": job.workflow_type,
                        "retry_of_attempt_no": attempt.retry_of_attempt_no,
                        "retry_of_job_id": job.retry_of_job_id,
                        "status": job.status,
                        "effective_priority": self.priority_name(job.priority),
                        "cancel_requested": job.cancel_requested_at is not None,
                        "can_retry": can_retry(JobStatus(job.status), category)
                        and job.cancel_requested_at is None,
                        "error": error,
                        "created_at": job.created_at,
                        "updated_at": job.updated_at,
                        "outputs": assets,
                    }
                )
            items.append(
                {
                    "item_id": item.id,
                    "position": item.position,
                    "current_attempt_no": None if not attempts else attempts[-1].attempt_no,
                    "attempts": histories,
                }
            )
        return {"batch_id": batch_id, "items": items}

    @staticmethod
    def _current_jobs(snapshot: dict[str, Any]) -> dict[str, Any]:
        current = {}
        jobs = {job.job_id: job for job in snapshot["jobs"]}
        for attempt in snapshot["attempts"]:
            if attempt.media_job_id in jobs:
                current[attempt.item_id] = jobs[attempt.media_job_id]
        return current

    @staticmethod
    def _operation_hash(batch_id: str, expected_version: int) -> str:
        canonical = json.dumps(
            {"batch_id": batch_id, "expected_batch_version": expected_version},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def _prepare(
        self,
        batch_id: str,
        *,
        expected_version: int,
        select_node: bool,
        require_draft: bool = True,
    ) -> PreparedBatch:
        batch = await self.repository.get_batch(batch_id)
        issues: list[dict[str, Any]] = []
        if batch.archived_at is not None or (require_draft and batch.state != "draft"):
            issues.append(self._issue("batch_not_editable", "Batch must be an active draft."))
        if batch.version != expected_version:
            issues.append(self._issue("version_conflict", "Batch version has changed."))
        rows = await self.repository.list_items(batch_id)
        if not 1 <= len(rows) <= 100:
            issues.append(self._issue("item_count_invalid", "Batch must contain 1 to 100 items."))
        spec = WORKFLOW_SPECS.get(batch.workflow_type)
        if spec is None:
            issues.append(self._issue("workflow_not_allowed", "Workflow is not supported."))
        node_id = None
        if (
            spec is not None
            and select_node
            and self.config.enabled
            and self.config.private_comfyui_enabled
        ):
            try:
                node_id = self.node_selector(batch.workflow_type)
            except RuntimeError:
                issues.append(
                    self._issue("no_enabled_node", "No enabled node supports this workflow.")
                )
        elif spec is not None and select_node:
            issues.append(self._issue("no_enabled_node", "No enabled node supports this workflow."))

        prepared: list[PreparedItem] = []
        for row in rows:
            overrides = dict(row.parameter_overrides_json)
            try:
                effective = freeze_generation_parameters(
                    dict(batch.common_parameters_json), overrides
                )
                self._validate_draft_parameters(effective)
            except ValueError:
                effective = {}
                issues.append(
                    self._issue(
                        "parameters_invalid",
                        "Generation parameters are invalid.",
                        row,
                        "parameters",
                    )
                )
            relations = await self.repository.list_item_assets(row.id)
            assets = tuple(
                DraftAssetReference(item.asset_id, item.role, item.position) for item in relations
            )
            if spec is not None:
                asset_id = assets[0].asset_id if len(assets) == 1 else None
                try:
                    MediaJobRequest.model_validate(
                        {
                            "workflow": batch.workflow_type,
                            "parameters": effective,
                            "asset_id": asset_id,
                        }
                    )
                except ValidationError:
                    issues.append(
                        self._issue(
                            "item_contract_invalid",
                            "Item does not match the workflow contract.",
                            row,
                        )
                    )
            expected_assets = 1 if spec is not None and spec.requires_image else 0
            if len(assets) != expected_assets or any(
                asset.role != "input_image" or asset.position != 0 for asset in assets
            ):
                issues.append(
                    self._issue(
                        "asset_arity_invalid",
                        "Managed asset references do not match the workflow.",
                        row,
                        "assets",
                    )
                )
            for reference in assets:
                try:
                    asset = await self._readable_input_asset(reference.asset_id)
                    if asset.media_type != "image" or not asset.mime_type.startswith("image/"):
                        raise ValueError
                except (AssetNotFoundError, ValueError, RuntimeError):
                    issues.append(
                        self._issue(
                            "asset_unavailable",
                            "A managed input asset is unavailable.",
                            row,
                            "assets",
                        )
                    )
            try:
                priority = int(
                    effective_priority(
                        Priority(batch.default_priority),
                        None if row.priority_override is None else Priority(row.priority_override),
                    )
                )
            except ValueError:
                priority = 1
                issues.append(
                    self._issue("priority_invalid", "Item priority is invalid.", row, "priority")
                )
            prepared.append(
                PreparedItem(
                    item_id=row.id,
                    position=row.position,
                    overrides=overrides,
                    effective_parameters=effective,
                    priority_override=row.priority_override,
                    priority=priority,
                    assets=assets,
                )
            )
        return PreparedBatch(batch, tuple(prepared), tuple(issues), node_id)

    async def _readable_input_asset(self, asset_id: str):
        asset = await self.assets.get(asset_id)
        if asset.kind != AssetKind.INPUT.value or asset.state != AssetState.AVAILABLE.value:
            raise RuntimeError("asset unavailable")
        try:
            stream = self.assets.store.open(asset.object_key)
        except (FileNotFoundError, ValueError):
            raise RuntimeError("asset unavailable") from None
        stream.close()
        return asset

    @staticmethod
    def _require_workflow(workflow: str) -> None:
        if workflow not in WORKFLOW_SPECS:
            raise ValueError("workflow is not allowed")

    @staticmethod
    def _validate_draft_parameters(parameters: dict[str, Any]) -> None:
        reject_secret_fields(parameters)
        if any(key in _FORBIDDEN_DRAFT_KEYS for key in parameters):
            raise ValueError("internal or scheduling fields are not draft parameters")

    @staticmethod
    def _issue(code: str, message: str, row=None, field: str | None = None):
        return {
            "code": code,
            "message": message,
            "item_id": None if row is None else row.id,
            "position": None if row is None else row.position,
            "field": field,
        }

    @staticmethod
    def _canonical_hash(prepared: PreparedBatch, expected_version: int) -> str:
        payload = {
            "batch_id": prepared.batch.id,
            "workflow": prepared.batch.workflow_type,
            "common_parameters": prepared.batch.common_parameters_json,
            "expected_batch_version": expected_version,
            "default_priority": prepared.batch.default_priority,
            "item_limit_snapshot": 100,
            "items": [
                {
                    "item_id": item.item_id,
                    "position": item.position,
                    "overrides": item.overrides,
                    "effective_parameters": item.effective_parameters,
                    "assets": [asset.__dict__ for asset in item.assets],
                    "priority_override": item.priority_override,
                    "effective_priority": item.priority,
                }
                for item in sorted(
                    prepared.items, key=lambda value: (value.position, value.item_id)
                )
            ],
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
