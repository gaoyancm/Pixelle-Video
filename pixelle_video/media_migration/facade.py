"""Closed, side-effect-free routing rules for staged legacy-entry migration.

Phase 02-E1 establishes this boundary without attaching existing public entry
points to it. Later batches may adapt an entry point to ``SubmissionRequest``
and provide exactly one submitter for each supported route.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping

from pydantic import ValidationError

from api.schemas.media_jobs import MediaJobRequest


class MigrationRoute(str, Enum):
    """The three exhaustive compatibility outcomes required by phase 02-E."""

    PERSISTENT_LEAF = "persistent_leaf"
    LEGACY_COMPOSITE = "legacy_composite"
    UNSUPPORTED_OR_INVALID = "unsupported_or_invalid"


@dataclass(frozen=True)
class SubmissionRequest:
    """Normalized intent presented by an explicitly adapted entry point."""

    entry_point: str
    workflow: str | None = None
    parameters: Mapping[str, Any] | None = None
    asset_id: str | None = None
    is_legacy_composite: bool = False


@dataclass(frozen=True)
class MigrationDecision:
    route: MigrationRoute
    request: MediaJobRequest | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class PersistentLeafSubmission:
    request: MediaJobRequest


@dataclass(frozen=True)
class LegacyCompositeSubmission:
    request: SubmissionRequest


class InvalidSubmissionError(ValueError):
    """Stable internal error for unsupported or invalid normalized requests."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


PersistentSubmitter = Callable[[PersistentLeafSubmission], Awaitable[Any]]
LegacySubmitter = Callable[[LegacyCompositeSubmission], Awaitable[Any]]


def classify_submission(request: SubmissionRequest) -> MigrationDecision:
    """Classify before submission, without fallback or execution side effects."""

    if request.is_legacy_composite:
        if request.workflow is not None or request.parameters is not None or request.asset_id is not None:
            return MigrationDecision(
                MigrationRoute.UNSUPPORTED_OR_INVALID,
                error_code="ambiguous_submission",
            )
        return MigrationDecision(MigrationRoute.LEGACY_COMPOSITE)

    if request.workflow is None or request.parameters is None:
        return MigrationDecision(
            MigrationRoute.UNSUPPORTED_OR_INVALID,
            error_code="incomplete_leaf_request",
        )

    try:
        leaf = MediaJobRequest.model_validate(
            {
                "workflow": request.workflow,
                "parameters": dict(request.parameters),
                "asset_id": request.asset_id,
            }
        )
    except ValidationError:
        return MigrationDecision(
            MigrationRoute.UNSUPPORTED_OR_INVALID,
            error_code="invalid_leaf_request",
        )
    return MigrationDecision(MigrationRoute.PERSISTENT_LEAF, request=leaf)


class CompatibilitySubmissionFacade:
    """Dispatch a pre-classified request to exactly one injected submitter."""

    def __init__(
        self,
        *,
        submit_persistent_leaf: PersistentSubmitter,
        submit_legacy_composite: LegacySubmitter,
    ):
        self._submit_persistent_leaf = submit_persistent_leaf
        self._submit_legacy_composite = submit_legacy_composite

    async def submit(self, request: SubmissionRequest) -> Any:
        decision = classify_submission(request)
        if decision.route is MigrationRoute.PERSISTENT_LEAF:
            assert decision.request is not None
            return await self._submit_persistent_leaf(
                PersistentLeafSubmission(decision.request)
            )
        if decision.route is MigrationRoute.LEGACY_COMPOSITE:
            return await self._submit_legacy_composite(
                LegacyCompositeSubmission(request)
            )
        raise InvalidSubmissionError(decision.error_code or "invalid_submission")
