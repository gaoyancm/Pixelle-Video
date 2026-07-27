from __future__ import annotations

from collections import Counter

import pytest

from pixelle_video.media_jobs.state_machine import ErrorCategory
from pixelle_video.media_migration.facade import (
    CompatibilitySubmissionFacade,
    InvalidSubmissionError,
    MigrationRoute,
    SubmissionRequest,
    classify_submission,
)


@pytest.mark.parametrize(
    ("workflow", "asset_id"),
    [
        ("a800_wan22_t2v_33f", None),
        ("a800_wan22_t2v_81f", None),
        ("gpu_4090_wan21_i2v_33f", "asset-1"),
        ("gpu_4090_wan21_i2v_81f", "asset-1"),
    ],
)
def test_each_registered_leaf_is_explicitly_migratable(workflow, asset_id):
    decision = classify_submission(
        SubmissionRequest(
            entry_point="library.media",
            workflow=workflow,
            parameters={"prompt": "safe prompt"},
            asset_id=asset_id,
        )
    )
    assert decision.route is MigrationRoute.PERSISTENT_LEAF
    assert decision.request is not None
    assert decision.request.workflow == workflow


@pytest.mark.parametrize(
    "submission",
    [
        SubmissionRequest(entry_point="library.media", workflow="a800_wan22_t2v_33f"),
        SubmissionRequest(
            entry_point="library.media",
            workflow="a800_wan22_t2v_33f",
            parameters={},
        ),
        SubmissionRequest(
            entry_point="library.media",
            workflow="unknown",
            parameters={"prompt": "safe prompt"},
        ),
        SubmissionRequest(
            entry_point="library.media",
            workflow="gpu_4090_wan21_i2v_33f",
            parameters={"prompt": "safe prompt"},
        ),
        SubmissionRequest(
            entry_point="library.media",
            workflow="a800_wan22_t2v_33f",
            parameters={"prompt": "safe prompt"},
            asset_id="unexpected",
        ),
    ],
)
def test_missing_or_invalid_leaf_contract_is_not_migratable(submission):
    assert classify_submission(submission).route is MigrationRoute.UNSUPPORTED_OR_INVALID


def test_complete_legacy_async_request_stays_composite():
    decision = classify_submission(
        SubmissionRequest(
            entry_point="api.video.generate.async",
            is_legacy_composite=True,
        )
    )
    assert decision.route is MigrationRoute.LEGACY_COMPOSITE


def test_sync_legacy_entry_stays_composite():
    decision = classify_submission(
        SubmissionRequest(
            entry_point="api.video.generate.sync",
            is_legacy_composite=True,
        )
    )
    assert decision.route is MigrationRoute.LEGACY_COMPOSITE


def test_ambiguous_composite_and_leaf_intent_is_rejected_before_submission():
    decision = classify_submission(
        SubmissionRequest(
            entry_point="ambiguous",
            is_legacy_composite=True,
            workflow="a800_wan22_t2v_33f",
            parameters={"prompt": "safe prompt"},
        )
    )
    assert decision.route is MigrationRoute.UNSUPPORTED_OR_INVALID
    assert decision.error_code == "ambiguous_submission"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("submission", "expected"),
    [
        (
            SubmissionRequest(
                entry_point="library.media",
                workflow="a800_wan22_t2v_33f",
                parameters={"prompt": "safe prompt"},
            ),
            "persistent",
        ),
        (
            SubmissionRequest(
                entry_point="api.video.generate.async",
                is_legacy_composite=True,
            ),
            "legacy",
        ),
    ],
)
async def test_facade_dispatches_to_exactly_one_route(submission, expected):
    calls = Counter()

    async def persistent(_submission):
        calls["persistent"] += 1
        return "persistent"

    async def legacy(_submission):
        calls["legacy"] += 1
        return "legacy"

    result = await CompatibilitySubmissionFacade(
        submit_persistent_leaf=persistent,
        submit_legacy_composite=legacy,
    ).submit(submission)

    assert result == expected
    assert calls == Counter({expected: 1})


@pytest.mark.asyncio
async def test_invalid_request_calls_neither_route():
    calls = Counter()

    async def persistent(_submission):
        calls["persistent"] += 1

    async def legacy(_submission):
        calls["legacy"] += 1

    facade = CompatibilitySubmissionFacade(
        submit_persistent_leaf=persistent,
        submit_legacy_composite=legacy,
    )
    with pytest.raises(InvalidSubmissionError, match="invalid_leaf_request"):
        await facade.submit(
            SubmissionRequest(
                entry_point="library.media",
                workflow="unknown",
                parameters={"prompt": "safe prompt"},
            )
        )
    assert not calls


@pytest.mark.asyncio
async def test_provider_submission_failure_propagates_without_fallback_or_double_submit():
    calls = Counter()
    failure = RuntimeError("provider submission failed")

    async def persistent(_submission):
        calls["persistent"] += 1
        raise failure

    async def legacy(_submission):
        calls["legacy"] += 1
        return "must not run"

    facade = CompatibilitySubmissionFacade(
        submit_persistent_leaf=persistent,
        submit_legacy_composite=legacy,
    )
    with pytest.raises(RuntimeError) as caught:
        await facade.submit(
            SubmissionRequest(
                entry_point="library.media",
                workflow="a800_wan22_t2v_33f",
                parameters={"prompt": "safe prompt"},
            )
        )
    assert caught.value is failure
    assert calls == Counter({"persistent": 1})


@pytest.mark.asyncio
async def test_submission_unknown_is_preserved_without_fallback_or_repeat_submit():
    calls = Counter()

    async def persistent(_submission):
        calls["persistent"] += 1
        return ErrorCategory.SUBMISSION_UNKNOWN

    async def legacy(_submission):
        calls["legacy"] += 1
        return "must not run"

    result = await CompatibilitySubmissionFacade(
        submit_persistent_leaf=persistent,
        submit_legacy_composite=legacy,
    ).submit(
        SubmissionRequest(
            entry_point="library.media",
            workflow="a800_wan22_t2v_33f",
            parameters={"prompt": "safe prompt"},
        )
    )
    assert result is ErrorCategory.SUBMISSION_UNKNOWN
    assert calls == Counter({"persistent": 1})
