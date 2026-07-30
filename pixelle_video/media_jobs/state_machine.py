"""Pure media-job state machine with no framework or provider dependencies."""

from __future__ import annotations

from datetime import datetime
from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    SUBMITTING = "submitting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ErrorCategory(str, Enum):
    VALIDATION = "validation"
    CONFIGURATION = "configuration"
    NODE_UNAVAILABLE = "node_unavailable"
    SUBMISSION_UNKNOWN = "submission_unknown"
    REMOTE_FAILED = "remote_failed"
    PROVIDER_REJECTED = "provider_rejected"
    OUTPUT_MISSING = "output_missing"
    STORAGE_FAILED = "storage_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


class RemoteJobStatus(str, Enum):
    UNKNOWN = "unknown"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RemoteTerminationStatus(str, Enum):
    UNKNOWN = "unknown"
    CANCELLATION_REQUESTED = "cancellation_requested"
    CANCELLED = "cancelled"
    STILL_RUNNING = "still_running"
    TERMINATION_UNKNOWN = "termination_unknown"


TERMINAL_STATUSES = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.TIMED_OUT,
    }
)

LEGAL_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset(
        {
            JobStatus.SUBMITTING,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.TIMED_OUT,
        }
    ),
    JobStatus.SUBMITTING: frozenset(
        {
            JobStatus.RUNNING,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.TIMED_OUT,
        }
    ),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.TIMED_OUT,
        }
    ),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.TIMED_OUT: frozenset(),
}

_RETRYABLE_ERRORS = frozenset(
    {
        ErrorCategory.NODE_UNAVAILABLE,
        ErrorCategory.REMOTE_FAILED,
        ErrorCategory.STORAGE_FAILED,
        ErrorCategory.INTERNAL,
    }
)


class InvalidStateTransition(ValueError):
    """Raised when a requested platform-state transition is illegal."""


def validate_transition(current: JobStatus, target: JobStatus) -> None:
    """Raise when ``current -> target`` is not a legal state transition."""

    if target not in LEGAL_TRANSITIONS[current]:
        raise InvalidStateTransition(f"illegal media-job transition: {current} -> {target}")


def is_terminal(status: JobStatus) -> bool:
    return status in TERMINAL_STATUSES


def can_cancel(status: JobStatus) -> bool:
    return status in {JobStatus.QUEUED, JobStatus.SUBMITTING, JobStatus.RUNNING}


def can_retry(status: JobStatus, error_category: ErrorCategory | None) -> bool:
    """Return whether a user may create a new job from a failed job.

    This never revives the original row. Submission uncertainty is deliberately
    excluded because the remote system may already have accepted the work.
    """

    return status is JobStatus.FAILED and error_category in _RETRYABLE_ERRORS


def is_submission_uncertain(
    status: JobStatus,
    submit_started_at: datetime | None,
    comfyui_prompt_id: str | None,
) -> bool:
    """Return whether a crash may have happened after a remote submit began."""

    return (
        status is JobStatus.SUBMITTING
        and submit_started_at is not None
        and not comfyui_prompt_id
    )
