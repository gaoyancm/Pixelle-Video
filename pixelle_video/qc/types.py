"""Shared dataclasses for the phase 04-B QC pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class QCIssue:
    rule_id: str
    severity: str  # critical | major | minor
    category: str
    field: str
    expected: Any
    actual: Any
    message: str


@dataclass
class QCResult:
    job_id: str
    executed_at: datetime
    profile: str
    total_rules: int
    issues: list[QCIssue] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return self.total_rules - len(self.issues)

    def to_payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "executed_at": self.executed_at.isoformat(),
            "profile": self.profile,
            "total_rules": self.total_rules,
            "passed": self.passed,
            "issues": [
                {
                    "rule_id": issue.rule_id,
                    "severity": issue.severity,
                    "category": issue.category,
                    "field": issue.field,
                    "expected": issue.expected,
                    "actual": issue.actual,
                    "message": issue.message,
                }
                for issue in self.issues
            ],
        }


@dataclass(frozen=True)
class QCDecision:
    decision: str  # pass | pass_with_warnings | human_review | partial_redo | full_reject
    summary: str


@dataclass(frozen=True)
class QCDiagnosis:
    issue_type: str
    symptom: str
    root_cause: str
    fix: str
