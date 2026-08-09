"""Rule evaluation and ffprobe-based technical probing for the 04-B QC engine."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Mapping

from pixelle_video.qc.types import QCIssue

_FFPROBE_TIMEOUT_SECONDS = 15.0
_RESOLUTION_RE = re.compile(r"^(\d{1,5})\s*[xX×]\s*(\d{1,5})$")


class ProbeError(RuntimeError):
    """Raised when ffprobe cannot inspect a media file."""


async def probe_with_ffprobe(path: str | Path) -> dict[str, Any]:
    """Extract technical evidence from a media file using the ffprobe binary."""
    ffprobe = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            ffprobe.communicate(), timeout=_FFPROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        ffprobe.kill()
        raise ProbeError("ffprobe timed out") from None
    if ffprobe.returncode != 0:
        detail = (stderr or b"").decode("utf-8", errors="replace")[:200]
        raise ProbeError(f"ffprobe failed: {detail}")
    payload = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
    streams = payload.get("streams") or []
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    fmt = payload.get("format") or {}

    resolution = None
    if video_stream is not None:
        width = video_stream.get("width")
        height = video_stream.get("height")
        if width and height:
            resolution = f"{width}x{height}"

    frame_rate = None
    if video_stream is not None:
        frame_rate = _parse_rate(video_stream.get("avg_frame_rate"))
        if frame_rate is None:
            frame_rate = _parse_rate(video_stream.get("r_frame_rate"))

    return {
        "resolution": resolution,
        "frame_rate": frame_rate,
        "duration_seconds": _to_float(fmt.get("duration")),
        "size_bytes": _to_int(fmt.get("size")),
        "audio_channels": (
            _to_int(audio_stream.get("channels")) if audio_stream is not None else None
        ),
        "has_video": video_stream is not None,
        "has_audio": audio_stream is not None,
    }


def _parse_rate(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        denominator = denominator.strip() or "1"
        try:
            return round(float(numerator) / float(denominator), 3)
        except (TypeError, ValueError):
            return None
    try:
        return round(float(text), 3)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return round(float(value), 3) if value is not None else None
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def evaluate_rule(
    rule: Any,
    evidence: Mapping[str, Any],
    *,
    severity_override: str | None = None,
) -> QCIssue | None:
    """Evaluate one rule against the provided evidence.

    Returns a QCIssue when the rule fails, or None when it passes. Accepts
    either a plain mapping (tests) or a QCRule ORM object.
    """
    rule_id = _get(rule, "id")
    name = _get(rule, "name")
    category = _get(rule, "category")
    config = dict(_get(rule, "rule_config_json") or {})
    field = str(config.get("field") or "")
    operator = str(config.get("operator") or "eq")
    expected = config.get("expected")
    severity = severity_override or str(config.get("severity") or "minor")
    actual = evidence.get(field)
    if actual is None:
        # Missing evidence cannot be judged; report it as a minor warning.
        return QCIssue(
            rule_id=str(rule_id),
            severity="minor",
            category=str(category or ""),
            field=field,
            expected=expected,
            actual=None,
            message=f"QC 规则 {name} 无法获取字段 {field}，缺少检查依据",
        )
    if _passes(operator, expected, actual):
        return None
    return QCIssue(
        rule_id=str(rule_id),
        severity=severity,
        category=str(category or ""),
        field=field,
        expected=expected,
        actual=actual,
        message=f"QC 规则 {name} 未通过：{field} 期望 {expected}，实际 {actual}",
    )


def _get(rule: Any, key: str, default: Any = None) -> Any:
    if isinstance(rule, Mapping):
        return rule.get(key, default)
    return getattr(rule, key, default)


def _passes(operator: str, expected: Any, actual: Any) -> bool:
    if operator == "eq":
        return _normalize(actual) == _normalize(expected)
    if operator == "in":
        allowed = [item.strip() for item in str(expected or "").split(",") if item.strip()]
        return _normalize(actual) in {_normalize(item) for item in allowed}
    if operator == "min":
        if isinstance(actual, str) and isinstance(expected, str):
            actual_size = _parse_resolution(actual)
            expected_size = _parse_resolution(expected)
            if actual_size and expected_size:
                return actual_size[0] >= expected_size[0] and actual_size[1] >= expected_size[1]
        return (
            _to_float(actual) is not None
            and _to_float(expected) is not None
            and (float(actual) >= float(expected))
        )
    if operator == "max":
        return (
            _to_float(actual) is not None
            and _to_float(expected) is not None
            and (float(actual) <= float(expected))
        )
    if operator == "range":
        low, _, high = str(expected or "").partition("-")
        numeric = _to_float(actual)
        if numeric is None:
            return False
        try:
            return float(low) <= numeric <= float(high)
        except (TypeError, ValueError):
            return False
    return _normalize(actual) == _normalize(expected)


def _parse_resolution(value: str) -> tuple[int, int] | None:
    match = _RESOLUTION_RE.match(value.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _normalize(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value or "").strip().lower()
