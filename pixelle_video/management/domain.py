"""Provider-independent rules for the phase 03 management domain."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any, Iterable, Mapping, Protocol


class Priority(IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2


class BatchState(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"


class ManagementOperationType(str, Enum):
    BATCH_SUBMIT = "batch_submit"
    BATCH_CANCEL = "batch_cancel"
    RETRY_ELIGIBLE = "retry_eligible"


PRIORITY_BY_NAME = {
    "low": Priority.LOW,
    "normal": Priority.NORMAL,
    "high": Priority.HIGH,
}


class AttemptLike(Protocol):
    attempt_no: int


def normalize_priority(value: int | str | Priority) -> Priority:
    """Return the stable low/normal/high numeric priority."""

    if isinstance(value, str):
        try:
            return PRIORITY_BY_NAME[value.strip().lower()]
        except KeyError:
            raise ValueError("priority must be low, normal, or high") from None
    try:
        return Priority(value)
    except (TypeError, ValueError):
        raise ValueError("priority must be 0, 1, or 2") from None


def effective_priority(
    default_priority: int | Priority,
    priority_override: int | Priority | None,
) -> Priority:
    """Resolve an item's scheduling priority independently of generation inputs."""

    return normalize_priority(default_priority if priority_override is None else priority_override)


def is_archived(archived_at: object | None) -> bool:
    return archived_at is not None


def batch_content_is_editable(*, state: str, archived_at: object | None) -> bool:
    return state == BatchState.DRAFT.value and not is_archived(archived_at)


def freeze_generation_parameters(
    common_parameters: Mapping[str, Any],
    parameter_overrides: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge the immutable generation snapshot without scheduling priority."""

    merged = {**common_parameters, **parameter_overrides}
    if "priority" in merged or "priority_override" in merged:
        raise ValueError("scheduling priority is not a generation parameter")
    return merged


def select_current_attempt(attempts: Iterable[AttemptLike]) -> AttemptLike | None:
    """Select the current attempt solely by the greatest positive attempt number."""

    current: AttemptLike | None = None
    for attempt in attempts:
        if attempt.attempt_no < 1:
            raise ValueError("attempt_no must be greater than zero")
        if current is None or attempt.attempt_no > current.attempt_no:
            current = attempt
    return current


@dataclass(frozen=True)
class OperationScopeKey:
    scope_type: str
    scope_id: str
    operation_type: str
    idempotency_key: str


def normalize_operation_scope_key(
    *,
    scope_type: str,
    scope_id: str,
    operation_type: str | ManagementOperationType,
    idempotency_key: str,
) -> OperationScopeKey:
    values = {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "idempotency_key": idempotency_key,
    }
    normalized = {name: value.strip() for name, value in values.items()}
    if any(not value for value in normalized.values()):
        raise ValueError("management operation scope values must not be blank")
    try:
        operation = ManagementOperationType(operation_type).value
    except ValueError:
        raise ValueError("unsupported management operation type") from None
    return OperationScopeKey(operation_type=operation, **normalized)


def operation_hash_matches(existing_hash: str, request_hash: str) -> bool:
    if not existing_hash or not request_hash:
        raise ValueError("management operation request hash must not be blank")
    return existing_hash == request_hash
