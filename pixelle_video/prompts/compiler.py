"""Mustache-style prompt variable compiler for phase 04-A (P2).

Existing pipelines keep their hard-coded prompts; this compiler is a new,
optional capability they can adopt incrementally.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_VARIABLE_TYPES = {"string", "integer", "json"}


class PromptCompileError(ValueError):
    """Raised when a template cannot be compiled with the supplied variables."""


def extract_variables(template_text: str) -> set[str]:
    """Return the distinct variable names referenced by a template."""
    return set(_PLACEHOLDER.findall(template_text or ""))


def validate(
    template_text: str,
    variable_defs: Sequence[Mapping[str, Any]],
    supplied: Mapping[str, Any],
) -> list[str]:
    """Return a list of human-readable problems (empty when the input is valid).

    Checks referenced variables against the declared definitions, required
    presence, and declared type.
    """
    issues: list[str] = []
    definitions = {str(definition.get("name")): definition for definition in variable_defs}
    referenced = extract_variables(template_text)
    for name in sorted(referenced):
        definition = definitions.get(name)
        if definition is None:
            issues.append(f"variable '{name}' is referenced but not declared")
            continue
        value = supplied.get(name)
        has_default = definition.get("default") is not None
        if value is None and definition.get("required") and not has_default:
            issues.append(f"required variable '{name}' is missing")
            continue
        if value is not None:
            expected_type = str(definition.get("type") or "string")
            if not _value_matches_type(value, expected_type):
                issues.append(f"variable '{name}' must be of type {expected_type}")
    return issues


def _value_matches_type(value: Any, expected_type: str) -> bool:
    if expected_type not in _VARIABLE_TYPES:
        return True
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "json":
        try:
            json.dumps(value)
            return True
        except (TypeError, ValueError):
            return False
    return True


def compile(
    template_text: str,
    supplied: Mapping[str, Any],
    variable_defs: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Replace every ``{{name}}`` placeholder using the supplied values.

    Missing required variables (without a declared default) raise
    :class:`PromptCompileError`. Optional variables fall back to their
    declared default when not supplied.
    """
    template = template_text or ""
    if variable_defs is not None:
        issues = validate(template, variable_defs, supplied)
        if issues:
            raise PromptCompileError("; ".join(issues))
    definitions = (
        {str(definition.get("name")): definition for definition in variable_defs}
        if variable_defs is not None
        else {}
    )
    missing: list[str] = []

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name in supplied and supplied[name] is not None:
            value = supplied[name]
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        definition = definitions.get(name)
        if definition is not None and definition.get("default") is not None:
            default = definition["default"]
            return str(default)
        if definition is not None and not definition.get("required"):
            # Optional variable without a default: substitute an empty value.
            return ""
        missing.append(name)
        return match.group(0)

    result = _PLACEHOLDER.sub(replace, template)
    if missing:
        raise PromptCompileError("missing values for variables: " + ", ".join(sorted(set(missing))))
    return result
