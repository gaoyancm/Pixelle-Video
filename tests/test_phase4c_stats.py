"""Phase 04-C E3 experiment statistics and comparison tests."""

from __future__ import annotations

from pixelle_video.experiments.stats import ExperimentStats


def _payload(experiment_id: str, groups: list[dict]) -> dict:
    return {
        "experiment_id": experiment_id,
        "metric": "composite",
        "groups": groups,
    }


def test_stats_empty_payload() -> None:
    report = ExperimentStats().compare(_payload("e1", []))
    assert report["winner"] is None
    assert report["confidence"] == "无数据"


def test_stats_mean_median_std() -> None:
    report = ExperimentStats().compare(
        _payload(
            "e1",
            [
                {
                    "group_id": "g1",
                    "group_name": "对照组",
                    "jobs": [{"composite": 70.0}, {"composite": 80.0}, {"composite": 90.0}],
                }
            ],
        )
    )
    entry = report["groups"][0]
    assert entry["sample_size"] == 3
    assert entry["mean_score"] == 80.0
    assert entry["median_score"] == 80.0


def test_stats_below_five_claims_no_significance() -> None:
    report = ExperimentStats().compare(
        _payload(
            "e1",
            [
                {
                    "group_id": "g1",
                    "group_name": "对照组",
                    "jobs": [{"composite": 70.0}, {"composite": 80.0}],
                }
            ],
        )
    )
    assert "不宣称" in report["confidence"]
    assert report["winner"] is None


def test_stats_trend_at_five_samples() -> None:
    jobs = [{"composite": float(60 + index)} for index in range(5)]
    report = ExperimentStats().compare(
        _payload("e1", [{"group_id": "g1", "group_name": "A", "jobs": jobs}])
    )
    assert "趋势" in report["confidence"]


def test_stats_significant_at_thirty_samples() -> None:
    jobs = [{"composite": float(80.0)} for _ in range(30)]
    report = ExperimentStats().compare(
        _payload("e1", [{"group_id": "g1", "group_name": "A", "jobs": jobs}])
    )
    assert "显著" in report["confidence"]


def test_stats_winner_and_improvement() -> None:
    control_jobs = [{"composite": float(70.0)} for _ in range(8)]
    variant_jobs = [{"composite": float(85.0)} for _ in range(8)]
    report = ExperimentStats().compare(
        _payload(
            "e1",
            [
                {"group_id": "g1", "group_name": "对照组", "jobs": control_jobs},
                {"group_id": "g2", "group_name": "实验组V4", "jobs": variant_jobs},
            ],
        )
    )
    assert report["winner"] == "实验组V4"
    assert report["improvement"] is not None
    assert report["improvement"] > 20.0  # (85-70)/70 = 21.4%


def test_stats_std_deviation() -> None:
    report = ExperimentStats().compare(
        _payload(
            "e1",
            [
                {
                    "group_id": "g1",
                    "group_name": "对照组",
                    "jobs": [
                        {"composite": 60.0},
                        {"composite": 70.0},
                        {"composite": 80.0},
                        {"composite": 90.0},
                        {"composite": 100.0},
                    ],
                }
            ],
        )
    )
    entry = report["groups"][0]
    assert entry["std"] > 0.0


def test_stats_no_winner_below_threshold() -> None:
    """Single group with fewer than five samples never claims a winner."""
    report = ExperimentStats().compare(
        _payload(
            "e1",
            [
                {
                    "group_id": "g1",
                    "group_name": "对照组",
                    "jobs": [{"composite": 70.0}, {"composite": 80.0}],
                }
            ],
        )
    )
    assert report["winner"] is None
    assert "不宣称" in report["confidence"]
