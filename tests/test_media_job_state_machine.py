from datetime import datetime, timezone

import pytest

from pixelle_video.media_jobs.state_machine import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATUSES,
    ErrorCategory,
    InvalidStateTransition,
    JobStatus,
    RemoteJobStatus,
    RemoteTerminationStatus,
    can_cancel,
    can_retry,
    is_submission_uncertain,
    is_terminal,
    validate_transition,
)


def test_every_declared_transition_is_legal() -> None:
    for current, targets in LEGAL_TRANSITIONS.items():
        for target in targets:
            validate_transition(current, target)


def test_every_undeclared_transition_is_illegal() -> None:
    for current in JobStatus:
        for target in JobStatus:
            if target in LEGAL_TRANSITIONS[current]:
                continue
            with pytest.raises(InvalidStateTransition):
                validate_transition(current, target)


def test_submitting_to_queued_requires_repository_recovery_guard() -> None:
    with pytest.raises(InvalidStateTransition):
        validate_transition(JobStatus.SUBMITTING, JobStatus.QUEUED)


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES, key=lambda item: item.value))
def test_terminal_states_cannot_be_overwritten(status: JobStatus) -> None:
    assert is_terminal(status)
    assert LEGAL_TRANSITIONS[status] == frozenset()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (JobStatus.QUEUED, True),
        (JobStatus.SUBMITTING, True),
        (JobStatus.RUNNING, True),
        (JobStatus.SUCCEEDED, False),
        (JobStatus.FAILED, False),
        (JobStatus.CANCELLED, False),
        (JobStatus.TIMED_OUT, False),
    ],
)
def test_can_cancel(status: JobStatus, expected: bool) -> None:
    assert can_cancel(status) is expected


def test_can_retry_only_creates_a_new_job_for_safe_failure_categories() -> None:
    assert can_retry(JobStatus.FAILED, ErrorCategory.NODE_UNAVAILABLE)
    assert can_retry(JobStatus.FAILED, ErrorCategory.REMOTE_FAILED)
    assert not can_retry(JobStatus.FAILED, ErrorCategory.SUBMISSION_UNKNOWN)
    assert not can_retry(JobStatus.FAILED, ErrorCategory.VALIDATION)
    assert not can_retry(JobStatus.TIMED_OUT, ErrorCategory.TIMEOUT)
    assert not can_retry(JobStatus.CANCELLED, ErrorCategory.CANCELLED)


def test_submission_uncertainty_requires_durable_submit_marker_without_prompt_id() -> None:
    submit_started_at = datetime.now(timezone.utc)

    assert is_submission_uncertain(JobStatus.SUBMITTING, submit_started_at, None)
    assert not is_submission_uncertain(JobStatus.SUBMITTING, None, None)
    assert not is_submission_uncertain(
        JobStatus.SUBMITTING, submit_started_at, "known-prompt"
    )
    assert not is_submission_uncertain(JobStatus.RUNNING, submit_started_at, None)


def test_platform_terminal_state_does_not_claim_remote_termination() -> None:
    assert RemoteJobStatus.RUNNING.value == "running"
    assert RemoteTerminationStatus.STILL_RUNNING.value == "still_running"
    assert RemoteTerminationStatus.TERMINATION_UNKNOWN.value == "termination_unknown"
    assert JobStatus.CANCELLED.value != RemoteTerminationStatus.CANCELLED.value or (
        JobStatus.CANCELLED is not RemoteTerminationStatus.CANCELLED
    )
