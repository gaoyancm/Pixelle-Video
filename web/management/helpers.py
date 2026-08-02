"""Pure transformation and intent helpers for the management workbench."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterable, MutableMapping
from typing import Any

PRIORITIES = ("low", "normal", "high")
STATUS_LABELS = {
    "queued": "Queued",
    "submitting": "Submitting",
    "running": "Running",
    "cancel_requested": "Cancellation requested",
    "succeeded": "Succeeded",
    "failed": "Failed",
    "timed_out": "Timed out",
    "cancelled": "Cancelled",
}
_INTERNAL_KEYS = {
    "node_id",
    "provider",
    "executor_kind",
    "workflow_key",
    "base_url",
    "submission_token",
    "prompt_id",
}


def safe_filename(value: str, fallback: str = "download") -> str:
    name = re.split(r"[/\\]", value or "")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]+", "", name).strip().strip(".")
    name = re.sub(r'[<>:"|?*]+', "_", name)
    return name[:255] or fallback


def canonical_fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def intent_key(
    state: MutableMapping[str, Any],
    *,
    action: str,
    scope_id: str,
    fingerprint: str,
    key_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> str:
    slot = f"management_intent:{action}:{scope_id}"
    existing = state.get(slot)
    if isinstance(existing, dict) and existing.get("fingerprint") == fingerprint:
        return str(existing["key"])
    key = key_factory()
    state[slot] = {"fingerprint": fingerprint, "key": key, "completed": False}
    return key


def complete_intent(
    state: MutableMapping[str, Any], *, action: str, scope_id: str, key: str
) -> None:
    slot = f"management_intent:{action}:{scope_id}"
    existing = state.get(slot)
    if isinstance(existing, dict) and existing.get("key") == key:
        existing["completed"] = True


def group_preflight_issues(issues: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {"batch": [], "items": []}
    for issue in issues:
        target = (
            "items"
            if issue.get("item_id") is not None or issue.get("position") is not None
            else "batch"
        )
        grouped[target].append(dict(issue))
    grouped["items"].sort(
        key=lambda item: (
            item.get("position") is None,
            item.get("position") if item.get("position") is not None else 0,
            str(item.get("item_id") or ""),
            str(item.get("field") or ""),
        )
    )
    return grouped


def normalize_item_rows(
    rows: Iterable[dict[str, Any]], *, requires_image: bool
) -> list[dict[str, Any]]:
    rows = list(rows)
    if len(rows) > 100:
        raise ValueError("a draft cannot contain more than 100 logical items")
    normalized = []
    positions = set()
    for index, raw in enumerate(rows):
        position = int(raw.get("position", index))
        if position < 0 or position in positions:
            raise ValueError("item positions must be unique non-negative integers")
        positions.add(position)
        overrides = raw.get("parameter_overrides", raw.get("parameters", {}))
        if isinstance(overrides, str):
            overrides = json.loads(overrides or "{}")
        if not isinstance(overrides, dict) or _INTERNAL_KEYS.intersection(overrides):
            raise ValueError("item parameter overrides are invalid")
        priority = raw.get("priority_override") or None
        if priority not in {*PRIORITIES, None}:
            raise ValueError("item priority override is invalid")
        asset_id = raw.get("asset_id")
        assets = []
        if requires_image:
            if not asset_id:
                raise ValueError("image workflows require one managed image asset per item")
            assets = [{"asset_id": str(asset_id), "role": "input_image", "position": 0}]
        elif asset_id:
            raise ValueError("text workflows cannot include input assets")
        item = {
            "position": position,
            "parameter_overrides": overrides,
            "priority_override": priority,
            "assets": assets,
        }
        if raw.get("item_id"):
            item["item_id"] = str(raw["item_id"])
        normalized.append(item)
    return sorted(normalized, key=lambda item: (item["position"], item.get("item_id", "")))


def editor_rows(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in sorted(items, key=lambda value: (value["position"], value["item_id"])):
        assets = item.get("assets") or []
        rows.append(
            {
                "item_id": item["item_id"],
                "position": item["position"],
                "parameters": json.dumps(
                    item.get("parameter_overrides") or {}, ensure_ascii=False, sort_keys=True
                ),
                "priority_override": item.get("priority_override") or "",
                "asset_id": assets[0]["asset_id"] if assets else "",
            }
        )
    return rows


def media_renderer(mime_type: str) -> str:
    value = (mime_type or "").lower()
    if value.startswith("image/"):
        return "image"
    if value.startswith("video/"):
        return "video"
    if value.startswith("audio/"):
        return "audio"
    return "download"


def progress_ratio(progress: dict[str, Any]) -> float:
    total = int(progress.get("total_items") or 0)
    completed = int(progress.get("completed_items") or 0)
    return 0.0 if total <= 0 else min(max(completed / total, 0.0), 1.0)


def attempt_label(attempt: dict[str, Any], current_attempt_no: int | None) -> str:
    marker = " (current)" if attempt.get("attempt_no") == current_attempt_no else ""
    return f"Attempt {attempt.get('attempt_no', '?')}{marker} · {STATUS_LABELS.get(attempt.get('status'), attempt.get('status', 'unknown'))}"
