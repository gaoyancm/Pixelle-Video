"""Persistent media-job domain and database primitives."""

from .contracts import (
    MediaInputAsset,
    MediaJobCreate,
    MediaOutputMetadata,
    compute_request_hash,
)
from .database import MediaJobsDatabase, MediaJobsDisabledError, resolve_database_url
from .executor import ManagedAssetResolver, RecoverableComfyUIExecutor
from .models import Base, MediaJob
from .repository import (
    CASConflictError,
    CreateJobResult,
    IdempotencyConflictError,
    MediaJobRepository,
)
from .state_machine import (
    ErrorCategory,
    JobStatus,
    RemoteJobStatus,
    RemoteTerminationStatus,
    can_cancel,
    can_retry,
    is_submission_uncertain,
    is_terminal,
    validate_transition,
)
from .worker import LeaseHandle, LeaseLostError, MediaJobWorker, default_worker_id

__all__ = [
    "Base",
    "CASConflictError",
    "CreateJobResult",
    "ErrorCategory",
    "IdempotencyConflictError",
    "JobStatus",
    "LeaseHandle",
    "LeaseLostError",
    "ManagedAssetResolver",
    "MediaInputAsset",
    "MediaJob",
    "MediaJobCreate",
    "MediaJobRepository",
    "MediaJobWorker",
    "MediaJobsDatabase",
    "MediaJobsDisabledError",
    "MediaOutputMetadata",
    "RemoteJobStatus",
    "RemoteTerminationStatus",
    "RecoverableComfyUIExecutor",
    "can_cancel",
    "can_retry",
    "compute_request_hash",
    "default_worker_id",
    "is_submission_uncertain",
    "is_terminal",
    "resolve_database_url",
    "validate_transition",
]
