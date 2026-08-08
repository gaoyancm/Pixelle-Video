"""Output contract validation for phase 03-F (F3).

Validation is a diagnostic tool, never a permission gate: critical issues are
recorded but never block task completion. Severity follows the
Generative-Media-Skills acceptance matrix (critical / major / minor).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

Severity = str  # "critical" | "major" | "minor"

SEVERITIES = ("critical", "major", "minor")


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    field: str
    expected: Any
    actual: Any
    message: str


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    issues: tuple[ValidationIssue, ...] = field(default_factory=tuple)

    def to_payloads(self) -> list[dict[str, Any]]:
        return [
            {
                "severity": issue.severity,
                "field": issue.field,
                "expected": issue.expected,
                "actual": issue.actual,
                "message": issue.message,
            }
            for issue in self.issues
        ]


def _expected_output_rules(schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    expected = schema.get("expected_outputs")
    if not isinstance(expected, list) or not expected:
        return []
    rules = []
    for entry in expected:
        if isinstance(entry, dict):
            rules.append(entry)
    return rules


def _issue(
    severity: Severity,
    field: str,
    expected: Any,
    actual: Any,
    message: str,
) -> ValidationIssue:
    return ValidationIssue(
        severity=severity,
        field=field,
        expected=expected,
        actual=actual,
        message=message,
    )


def _critical(field: str, expected: Any, actual: Any, message: str) -> ValidationIssue:
    return _issue("critical", field, expected, actual, message)


def _major(field: str, expected: Any, actual: Any, message: str) -> ValidationIssue:
    return _issue("major", field, expected, actual, message)


def _matches_pattern(value: str | None, pattern: str | None) -> bool:
    if pattern is None:
        return True
    if value is None:
        return False
    try:
        return re.search(pattern, value) is not None
    except re.error:
        return False


def _in_range(
    value: float | int | None, minimum: float | int | None, maximum: float | int | None
) -> bool:
    if value is None:
        return minimum is None and maximum is None
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def validate_output(
    *,
    rule: Mapping[str, Any],
    output_index: int,
    exists: bool,
    mime_type: str | None,
    size_bytes: int | None,
    duration: float | None,
    width: int | None,
    height: int | None,
) -> list[ValidationIssue]:
    """Validate one output asset against one expected-output rule."""
    issues: list[ValidationIssue] = []
    prefix = f"outputs[{output_index}]"

    required = bool(rule.get("required", True))
    if required and not exists:
        issues.append(
            _critical(
                f"{prefix}.exists",
                True,
                False,
                "expected output asset is missing",
            )
        )
        return issues

    media_type = rule.get("media_type")
    if media_type is not None and mime_type is not None:
        expected_mime = rule.get("mime_type_pattern")
        if expected_mime is None:
            expected_mime = f"^{re.escape(media_type)}/"
        if not _matches_pattern(mime_type, expected_mime):
            issues.append(
                _major(
                    f"{prefix}.mime_type",
                    expected_mime,
                    mime_type,
                    "output mime type does not match the expected pattern",
                )
            )

    min_size = rule.get("min_size_bytes")
    max_size = rule.get("max_size_bytes")
    if (min_size is not None or max_size is not None) and not _in_range(
        size_bytes, min_size, max_size
    ):
        issues.append(
            _major(
                f"{prefix}.size_bytes",
                {"min": min_size, "max": max_size},
                size_bytes,
                "output size is outside the expected range",
            )
        )

    min_duration = rule.get("min_duration")
    max_duration = rule.get("max_duration")
    if (min_duration is not None or max_duration is not None) and not _in_range(
        duration, min_duration, max_duration
    ):
        issues.append(
            _major(
                f"{prefix}.duration",
                {"min": min_duration, "max": max_duration},
                duration,
                "output duration is outside the expected range",
            )
        )

    min_width = rule.get("min_width")
    min_height = rule.get("min_height")
    if (min_width is not None and (width is None or width < min_width)) or (
        min_height is not None and (height is None or height < min_height)
    ):
        issues.append(
            _major(
                f"{prefix}.resolution",
                {"min_width": min_width, "min_height": min_height},
                {"width": width, "height": height},
                "output resolution is below the expected minimum",
            )
        )

    return issues


def validate_outputs(
    *,
    schema: Mapping[str, Any],
    outputs: Sequence[Mapping[str, Any]],
) -> ValidationResult:
    """Validate a job's output metadata against a workflow output schema."""
    rules = _expected_output_rules(schema)
    if not rules:
        return ValidationResult(passed=True)
    issues: list[ValidationIssue] = []
    for index, rule in enumerate(rules):
        if index >= len(outputs):
            issues.append(
                _critical(
                    f"outputs[{index}].exists",
                    True,
                    False,
                    "expected output is missing",
                )
            )
            continue
        output = outputs[index]
        issues.extend(
            validate_output(
                rule=rule,
                output_index=index,
                exists=bool(output.get("exists")),
                mime_type=output.get("mime_type"),
                size_bytes=output.get("size_bytes"),
                duration=output.get("duration"),
                width=output.get("width"),
                height=output.get("height"),
            )
        )
    return ValidationResult(
        passed=not any(issue.severity == "critical" for issue in issues),
        issues=tuple(issues),
    )
