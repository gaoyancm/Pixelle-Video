"""Experiment comparison statistics for phase 04-C (E3)."""

from __future__ import annotations

import statistics
from typing import Any, Sequence

TREND_THRESHOLD = 5
SIGNIFICANT_THRESHOLD = 30


class ExperimentStats:
    """Aggregate composite scores and produce an A/B comparison report.

    Significance is labeled purely from sample size per the contract:
    >= 30 shows "显著", >= 5 shows "趋势", below 5 claims nothing.
    """

    @staticmethod
    def _mean(values: Sequence[float]) -> float:
        return round(statistics.mean(values), 2) if values else 0.0

    @staticmethod
    def _median(values: Sequence[float]) -> float:
        return round(statistics.median(values), 2) if values else 0.0

    @staticmethod
    def _std(values: Sequence[float]) -> float:
        return round(statistics.stdev(values), 2) if len(values) >= 2 else 0.0

    @staticmethod
    def _confidence(sample_size: int) -> str:
        if sample_size >= SIGNIFICANT_THRESHOLD:
            return "显著（样本量>=30）"
        if sample_size >= TREND_THRESHOLD:
            return "趋势（样本量<30）"
        return "样本量不足（<5，不宣称显著性）"

    def compare(self, experiment_payload: dict[str, Any]) -> dict[str, Any]:
        groups_payload = experiment_payload.get("groups") or []
        entries: list[dict[str, Any]] = []
        for group in groups_payload:
            scores = [job["composite"] for job in (group.get("jobs") or [])]
            entries.append(
                {
                    "group_id": group.get("group_id"),
                    "name": group.get("group_name"),
                    "sample_size": len(scores),
                    "mean_score": self._mean(scores),
                    "median_score": self._median(scores),
                    "std": self._std(scores),
                }
            )

        winner = None
        improvement = None
        if entries:
            best = max(entries, key=lambda entry: entry["mean_score"])
            if best["sample_size"] >= TREND_THRESHOLD:
                winner = best["name"]
                control = next(
                    (entry for entry in entries if entry["sample_size"] >= TREND_THRESHOLD),
                    None,
                )
                if (
                    control is not None
                    and control["mean_score"] > 0
                    and best["name"] != control["name"]
                ):
                    improvement = round(
                        (best["mean_score"] - control["mean_score"])
                        / control["mean_score"]
                        * 100.0,
                        1,
                    )

        return {
            "experiment_id": experiment_payload.get("experiment_id"),
            "metric": experiment_payload.get("metric", "composite"),
            "groups": entries,
            "winner": winner,
            "confidence": (
                self._confidence(max((entry["sample_size"] for entry in entries), default=0))
                if entries
                else "无数据"
            ),
            "improvement": improvement,
        }
