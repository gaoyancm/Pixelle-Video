from types import SimpleNamespace

import pytest

from pixelle_video.management.domain import (
    Priority,
    batch_content_is_editable,
    effective_priority,
    freeze_generation_parameters,
    is_archived,
    normalize_operation_scope_key,
    normalize_priority,
    operation_hash_matches,
    select_current_attempt,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("low", 0), ("normal", 1), ("high", 2), (0, 0), (1, 1), (2, 2)],
)
def test_priority_has_stable_mapping(value, expected):
    assert normalize_priority(value) is Priority(expected)


@pytest.mark.parametrize("value", [-1, 3, "urgent", None])
def test_priority_rejects_unsupported_values(value):
    with pytest.raises(ValueError, match="priority"):
        normalize_priority(value)


def test_effective_priority_prefers_nullable_item_override():
    assert effective_priority(Priority.NORMAL, None) is Priority.NORMAL
    assert effective_priority(Priority.NORMAL, Priority.HIGH) is Priority.HIGH


def test_batch_editability_and_archive_rules_are_explicit():
    assert batch_content_is_editable(state="draft", archived_at=None)
    assert not batch_content_is_editable(state="submitted", archived_at=None)
    assert not batch_content_is_editable(state="draft", archived_at=object())
    assert not is_archived(None)
    assert is_archived(object())


def test_generation_snapshot_is_separate_from_adjustable_priority():
    assert freeze_generation_parameters({"prompt": "base"}, {"seed": 7}) == {
        "prompt": "base",
        "seed": 7,
    }
    with pytest.raises(ValueError, match="scheduling priority"):
        freeze_generation_parameters({"priority": 2}, {})


def test_current_attempt_is_maximum_number_and_preserves_history():
    attempts = [SimpleNamespace(attempt_no=1), SimpleNamespace(attempt_no=3)]
    assert select_current_attempt(attempts) is attempts[1]
    assert select_current_attempt([]) is None
    with pytest.raises(ValueError, match="greater than zero"):
        select_current_attempt([SimpleNamespace(attempt_no=0)])


def test_management_scope_key_normalizes_and_hash_match_is_exact():
    key = normalize_operation_scope_key(
        scope_type=" batch ",
        scope_id=" id-1 ",
        operation_type="batch_submit",
        idempotency_key=" stable ",
    )
    assert key.scope_type == "batch"
    assert key.scope_id == "id-1"
    assert key.idempotency_key == "stable"
    assert operation_hash_matches("abc", "abc")
    assert not operation_hash_matches("abc", "def")
