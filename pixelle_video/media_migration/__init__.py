"""Compatibility boundary for staged migration of legacy media entry points."""

from .facade import (
    CompatibilitySubmissionFacade,
    InvalidSubmissionError,
    LegacyCompositeSubmission,
    MigrationDecision,
    MigrationRoute,
    PersistentLeafSubmission,
    SubmissionRequest,
)

__all__ = [
    "CompatibilitySubmissionFacade",
    "InvalidSubmissionError",
    "LegacyCompositeSubmission",
    "MigrationDecision",
    "MigrationRoute",
    "PersistentLeafSubmission",
    "SubmissionRequest",
]
