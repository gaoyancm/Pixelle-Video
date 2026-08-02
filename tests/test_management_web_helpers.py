from __future__ import annotations

import pytest

from web.management.helpers import (
    attempt_label,
    canonical_fingerprint,
    complete_intent,
    editor_rows,
    group_preflight_issues,
    intent_key,
    media_renderer,
    normalize_item_rows,
    progress_ratio,
    safe_filename,
)


def test_intent_key_reuses_same_fingerprint_and_rotates_only_when_scope_content_changes():
    state = {}
    values = iter(("key-1", "key-2", "key-3"))

    def factory():
        return next(values)

    first = intent_key(
        state, action="submit", scope_id="b1", fingerprint="hash-1", key_factory=factory
    )
    assert (
        intent_key(state, action="submit", scope_id="b1", fingerprint="hash-1", key_factory=factory)
        == first
    )
    complete_intent(state, action="submit", scope_id="b1", key=first)
    assert state["management_intent:submit:b1"]["completed"] is True
    assert (
        intent_key(state, action="submit", scope_id="b1", fingerprint="hash-2", key_factory=factory)
        == "key-2"
    )
    assert (
        intent_key(state, action="cancel", scope_id="b1", fingerprint="hash-2", key_factory=factory)
        == "key-3"
    )


def test_canonical_fingerprint_is_key_order_stable_and_content_sensitive():
    assert canonical_fingerprint({"b": 2, "a": [1]}) == canonical_fingerprint({"a": [1], "b": 2})
    assert canonical_fingerprint({"a": 1}) != canonical_fingerprint({"a": 2})


def test_item_serialization_preserves_ids_orders_positions_and_enforces_asset_arity():
    rows = [
        {
            "item_id": "i2",
            "position": 1,
            "parameters": '{"prompt":"second"}',
            "priority_override": "high",
            "asset_id": "asset-2",
        },
        {
            "item_id": "i1",
            "position": 0,
            "parameters": "{}",
            "priority_override": "",
            "asset_id": "asset-1",
        },
    ]
    payload = normalize_item_rows(rows, requires_image=True)
    assert [item["item_id"] for item in payload] == ["i1", "i2"]
    assert payload[0]["priority_override"] is None
    assert payload[1]["assets"] == [{"asset_id": "asset-2", "role": "input_image", "position": 0}]
    with pytest.raises(ValueError, match="cannot include"):
        normalize_item_rows(rows, requires_image=False)
    with pytest.raises(ValueError, match="require one"):
        normalize_item_rows([{**rows[0], "asset_id": ""}], requires_image=True)
    with pytest.raises(ValueError, match="unique"):
        normalize_item_rows([rows[0], {**rows[1], "position": 1}], requires_image=True)


def test_item_serialization_rejects_internal_fields_and_round_trips_server_rows():
    with pytest.raises(ValueError, match="invalid"):
        normalize_item_rows(
            [{"position": 0, "parameters": '{"node_id":"hidden"}'}],
            requires_image=False,
        )
    source = [
        {
            "item_id": "i1",
            "position": 0,
            "parameter_overrides": {"prompt": "safe"},
            "priority_override": None,
            "assets": [],
        }
    ]
    assert editor_rows(source) == [
        {
            "item_id": "i1",
            "position": 0,
            "parameters": '{"prompt": "safe"}',
            "priority_override": "",
            "asset_id": "",
        }
    ]


def test_item_serialization_accepts_zero_and_one_hundred_but_rejects_one_hundred_one():
    assert normalize_item_rows([], requires_image=False) == []
    hundred = [{"position": index, "parameters": "{}"} for index in range(100)]
    assert len(normalize_item_rows(hundred, requires_image=False)) == 100
    with pytest.raises(ValueError, match="more than 100"):
        normalize_item_rows([*hundred, {"position": 100}], requires_image=False)


def test_issue_grouping_status_media_filename_and_progress_are_truthful():
    issues = [
        {"code": "batch", "position": None},
        {"code": "row2", "position": 2, "item_id": "i2"},
        {"code": "row1", "position": 1, "item_id": "i1"},
    ]
    grouped = group_preflight_issues(issues)
    assert [item["code"] for item in grouped["batch"]] == ["batch"]
    assert [item["code"] for item in grouped["items"]] == ["row1", "row2"]
    assert media_renderer("image/png") == "image"
    assert media_renderer("video/mp4") == "video"
    assert media_renderer("audio/wav") == "audio"
    assert media_renderer("application/pdf") == "download"
    assert safe_filename("C:\\secret\\result.mp4\r\n") == "result.mp4"
    assert progress_ratio({"completed_items": 2, "total_items": 4}) == 0.5
    assert progress_ratio({"completed_items": 9, "total_items": 0}) == 0.0
    assert attempt_label({"attempt_no": 2, "status": "cancelled"}, 2) == (
        "Attempt 2 (current) · Cancelled"
    )
