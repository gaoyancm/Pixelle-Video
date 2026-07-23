"""Persistent media-job domain and database primitives."""

from .contracts import (
    MediaInputAsset,
    MediaJobCreate,
    MediaOutputMetadata,
    compute_request_hash,
)
from .database import MediaJobsDatabase, MediaJobsDisabledError, resolve_database_url
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

__all__ = [
    "Base",
    "CASConflictError",
    "CreateJobResult",
    "ErrorCategory",
    "IdempotencyConflictError",
    "JobStatus",
    "MediaInputAsset",
    "MediaJob",
    "MediaJobCreate",
    "MediaJobRepository",
    "MediaJobsDatabase",
    "MediaJobsDisabledError",
    "MediaOutputMetadata",
    "RemoteJobStatus",
    "RemoteTerminationStatus",
    "can_cancel",
    "can_retry",
    "compute_request_hash",
    "is_submission_uncertain",
    "is_terminal",
    "resolve_database_url",
    "validate_transition",
]
