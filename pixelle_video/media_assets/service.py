"""Asset lifecycle service coordinating database records and local bytes."""

from __future__ import annotations

import hashlib
import io
import mimetypes
from datetime import timedelta, timezone
from pathlib import Path

from pixelle_video.media_jobs.models import utc_now

from .contracts import (
    ALLOWED_MEDIA_TYPES,
    AssetKind,
    AssetSource,
    AssetState,
    ReconciliationReport,
    new_asset_id,
    require_asset_uuid,
)
from .models import MediaAsset
from .repository import AssetRepository
from .store import LocalAssetStore
from .validation import ValidationResult, validate_outputs


class AssetNotFoundError(RuntimeError):
    pass


class AssetUnavailableError(RuntimeError):
    pass


class UnsupportedMediaError(ValueError):
    pass


def classify_media(filename: str, declared_mime: str | None) -> tuple[str, str, str]:
    suffix = Path(filename or "").suffix.lower()
    guessed = mimetypes.guess_type(filename or "")[0]
    mime = (declared_mime or guessed or "").lower()
    for media_type, mappings in ALLOWED_MEDIA_TYPES.items():
        for accepted_mime, extensions in mappings.items():
            if suffix in extensions and (mime == accepted_mime or not mime):
                return media_type, accepted_mime, suffix
    raise UnsupportedMediaError("unsupported or inconsistent media type")


def content_matches_mime(prefix: bytes, mime_type: str) -> bool:
    signatures = {
        "image/png": lambda value: value.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": lambda value: value.startswith(b"\xff\xd8\xff"),
        "image/webp": lambda value: value.startswith(b"RIFF") and value[8:12] == b"WEBP",
        "video/mp4": lambda value: len(value) >= 12 and value[4:8] == b"ftyp",
        "video/quicktime": lambda value: len(value) >= 12 and value[4:8] == b"ftyp",
        "video/webm": lambda value: value.startswith(b"\x1aE\xdf\xa3"),
        "audio/mpeg": lambda value: value.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3")),
        "audio/wav": lambda value: value.startswith(b"RIFF") and value[8:12] == b"WAVE",
    }
    validator = signatures.get(mime_type)
    return bool(validator and validator(prefix))


class AssetService:
    def __init__(self, repository: AssetRepository, store: LocalAssetStore, *, max_upload_size: int):
        self.repository = repository
        self.store = store
        self.max_upload_size = max_upload_size

    async def upload(
        self,
        source,
        *,
        filename: str,
        mime_type: str | None,
        idempotency_key: str,
    ) -> tuple[MediaAsset, bool]:
        media_type, validated_mime, suffix = classify_media(filename, mime_type)
        asset_id = new_asset_id()
        object_key = self.store.new_object_key(asset_id, suffix)
        stored = self.store.write_stream(
            source, object_key=object_key, max_bytes=self.max_upload_size
        )
        with self.store.open(object_key) as uploaded:
            prefix = uploaded.read(16)
        if not content_matches_mime(prefix, validated_mime):
            self.store.delete(object_key)
            raise UnsupportedMediaError("asset content does not match its media type")
        request_hash = hashlib.sha256(
            f"{filename}\0{validated_mime}\0{stored.size_bytes}\0{stored.sha256}".encode("utf-8")
        ).hexdigest()
        asset = MediaAsset(
            id=asset_id,
            kind=AssetKind.INPUT.value,
            state=AssetState.AVAILABLE.value,
            backend="local",
            object_key=object_key,
            original_filename=Path(filename).name[:255] or "upload",
            media_type=media_type,
            mime_type=validated_mime,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            source=AssetSource.UPLOAD.value,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        try:
            persisted, created = await self.repository.create(asset)
        except Exception:
            self.store.delete(object_key)
            raise
        if not created:
            self.store.delete(object_key)
        return persisted, created

    async def get(self, asset_id: str) -> MediaAsset:
        require_asset_uuid(asset_id)
        asset = await self.repository.get(asset_id)
        if asset is None:
            raise AssetNotFoundError
        return asset

    async def get_available_input(self, asset_id: str) -> MediaAsset:
        asset = await self.get(asset_id)
        if asset.kind != AssetKind.INPUT.value or asset.state != AssetState.AVAILABLE.value:
            raise AssetUnavailableError
        if not self.store.exists(asset.object_key):
            await self.repository.mark_missing(asset.id)
            raise AssetUnavailableError
        return asset

    async def list(self, *, kind, state, limit: int, offset: int):
        rows = await self.repository.list(kind=kind, state=state, limit=limit + 1, offset=offset)
        return rows[:limit], len(rows) > limit

    async def open_content(self, asset_id: str):
        asset = await self.get(asset_id)
        if asset.state != AssetState.AVAILABLE.value:
            raise AssetUnavailableError
        try:
            stream = self.store.open(asset.object_key)
        except FileNotFoundError:
            await self.repository.mark_missing(asset.id)
            raise AssetUnavailableError from None
        return asset, stream

    async def resolve_local_input(self, asset_id: str) -> Path:
        asset = await self.get_available_input(asset_id)
        return self.store.local_path(asset.object_key)

    async def resolve_job_input(self, job_id: str) -> Path | None:
        assets = await self.repository.input_assets_for_job(job_id)
        if not assets:
            return None
        asset = assets[0]
        if asset.kind != AssetKind.INPUT.value or asset.state != AssetState.AVAILABLE.value:
            raise AssetUnavailableError
        if not self.store.exists(asset.object_key):
            await self.repository.mark_missing(asset.id)
            raise AssetUnavailableError
        return self.store.local_path(asset.object_key)

    async def validate_committed_output_group(self, job_id: str) -> bool:
        relations = await self.repository.output_assets_for_job(job_id)
        if not relations:
            return False
        job = await self.repository.get_job(job_id)
        if job is None or len(job.output_metadata) != len(relations):
            return False
        expected_positions = list(range(len(relations)))
        if [relation.position for relation, _asset in relations] != expected_positions:
            return False
        for position, (_relation, asset) in enumerate(relations):
            projection = job.output_metadata[position]
            if (
                asset.kind != AssetKind.OUTPUT.value
                or asset.state != AssetState.AVAILABLE.value
                or not self.store.exists(asset.object_key)
                or projection.get("output_id") != asset.id
                or projection.get("size") != asset.size_bytes
                or projection.get("sha256") != asset.sha256
                or projection.get("mime_type") != asset.mime_type
            ):
                return False
        return True

    async def validate_output_contract(self, job_id: str, schema: dict) -> ValidationResult:
        """Validate a completed job's committed outputs against an output schema.

        This is a diagnostic tool: it returns issues but never blocks completion.
        """
        from .validation import ValidationIssue

        relations = await self.repository.output_assets_for_job(job_id)
        job = await self.repository.get_job(job_id)
        if job is None or not relations:
            issue = ValidationIssue(
                severity="critical",
                field="outputs[0].exists",
                expected=True,
                actual=False,
                message="expected output asset is missing",
            )
            return ValidationResult(passed=False, issues=(issue,))
        projections = {entry.get("output_id"): entry for entry in job.output_metadata}
        outputs = []
        for relation, asset in relations:
            projection = projections.get(asset.id, {})
            outputs.append(
                {
                    "exists": self.store.exists(asset.object_key),
                    "mime_type": asset.mime_type,
                    "size_bytes": asset.size_bytes,
                    "duration": projection.get("duration"),
                    "width": projection.get("width"),
                    "height": projection.get("height"),
                }
            )
        return validate_outputs(schema=schema, outputs=outputs)

    async def validate_and_record_output_contract(
        self,
        job_id: str,
        *,
        audit=None,
        operator: str = "system",
    ) -> ValidationResult:
        """Look up the workflow schema, validate, and record every issue in the audit trail."""
        job = await self.repository.get_job(job_id)
        if job is None:
            return ValidationResult(passed=True)
        schema_row = await self.repository.get_output_schema(job.workflow_type)
        if schema_row is None:
            return ValidationResult(passed=True)
        result = await self.validate_output_contract(job_id, schema_row.schema_json)
        if audit is not None and result.issues:
            try:
                await audit.record(
                    event_type="output_validated",
                    scope_type="job",
                    scope_id=job_id,
                    operator=operator,
                    details={
                        "workflow_type": job.workflow_type,
                        "passed": result.passed,
                        "issues": result.to_payloads(),
                    },
                )
            except Exception:
                pass
        return result

    def discard_unregistered(self, asset: MediaAsset) -> None:
        if self.store.exists(asset.object_key):
            self.store.delete(asset.object_key)

    async def soft_delete(self, asset_id: str) -> MediaAsset:
        await self.get(asset_id)
        deleted = await self.repository.soft_delete(asset_id)
        assert deleted is not None
        return deleted

    async def physical_cleanup(self, asset_id: str) -> None:
        asset = await self.repository.get(asset_id)
        if asset is None:
            return
        object_exists = self.store.exists(asset.object_key)
        if asset.state == AssetState.DISABLED.value and object_exists:
            updated_at = asset.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if utc_now() - updated_at < timedelta(minutes=1):
                raise RuntimeError("asset cleanup is already in progress")
        elif asset.state != AssetState.DISABLED.value:
            asset = await self.repository.claim_cleanup(asset_id)
        if object_exists:
            self.store.delete(asset.object_key)
        await self.repository.purge_claimed_record(asset_id)

    async def register_generated_bytes(
        self,
        content: bytes,
        *,
        filename: str,
    ) -> MediaAsset:
        media_type, mime_type, suffix = classify_media(filename, mimetypes.guess_type(filename)[0])
        if not content_matches_mime(content[:16], mime_type):
            raise UnsupportedMediaError("generated content does not match its media type")
        asset_id = new_asset_id()
        object_key = self.store.new_object_key(asset_id, suffix)
        stored = self.store.write_stream(
            io.BytesIO(content), object_key=object_key, max_bytes=self.max_upload_size
        )
        return MediaAsset(
            id=asset_id,
            kind=AssetKind.OUTPUT.value,
            state=AssetState.AVAILABLE.value,
            backend="local",
            object_key=object_key,
            original_filename=Path(filename).name[:255],
            media_type=media_type,
            mime_type=mime_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            source=AssetSource.GENERATED.value,
        )

    async def reconcile(self, *, stale_after: timedelta = timedelta(hours=1)) -> ReconciliationReport:
        assets = await self.repository.all_assets()
        known_keys = {asset.object_key for asset in assets}
        missing = tuple(
            asset.id
            for asset in assets
            if asset.state == AssetState.AVAILABLE.value and not self.store.exists(asset.object_key)
        )
        orphans = tuple(sorted(set(self.store.iter_object_keys()) - known_keys))
        cutoff = (utc_now() - stale_after).timestamp()
        stale = tuple(sorted(self.store.iter_stale_temporary_keys(older_than_timestamp=cutoff)))
        return ReconciliationReport(
            missing_assets=missing,
            orphan_objects=orphans,
            stale_temporary_objects=stale,
            succeeded_jobs_without_outputs=tuple(
                await self.repository.succeeded_jobs_without_outputs()
            ),
            unavailable_output_relations=tuple(
                await self.repository.unavailable_output_relations()
            ),
        )
