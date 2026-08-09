"""Failure sample feedback for phase 04-C (E4).

Flags the worst-performing experiment jobs (bottom 20% composite, or any
job with a critical QC issue), records them as failure samples linked to
the prompt version they used, and keeps the QC issue details for reuse.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from pixelle_video.experiments.models import FailureSample
from pixelle_video.experiments.repository import ExperimentRepository

BOTTOM_PERCENT = 0.20


class FailureFeedbackEngine:
    """Identify and persist failure samples from an experiment payload."""

    def __init__(
        self,
        repository: ExperimentRepository,
        *,
        qc_issue_reader: Callable[[str], Awaitable[list[dict[str, Any]]]],
    ):
        self.repository = repository
        self._qc_issue_reader = qc_issue_reader

    async def collect_failures(
        self, experiment_id: str, experiment_payload: dict[str, Any]
    ) -> list[FailureSample]:
        jobs = self._flatten_jobs(experiment_payload)
        if not jobs:
            return []
        threshold = self._bottom_threshold(jobs)

        samples: list[FailureSample] = []
        seen: set[str] = set()
        for job in jobs:
            job_id = job["job_id"]
            qc_issues = await self._qc_issue_reader(job_id)
            has_critical = any(issue.get("severity") == "critical" for issue in qc_issues)
            low_score = job["composite"] <= threshold
            if not (low_score or has_critical):
                continue
            if job_id in seen:
                continue
            seen.add(job_id)
            reason = self._reason(job, has_critical, qc_issues)
            samples.append(
                await self.repository.record_failure(
                    job_id=job_id,
                    experiment_id=experiment_id,
                    prompt_version_id=job.get("prompt_version_id"),
                    reason=reason,
                    qc_issues_json=qc_issues or None,
                )
            )
        return samples

    @staticmethod
    def _flatten_jobs(payload: dict[str, Any]) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for group in payload.get("groups") or []:
            prompt_version_id = group.get("prompt_version_id")
            for job in group.get("jobs") or []:
                entry = dict(job)
                entry["prompt_version_id"] = prompt_version_id
                jobs.append(entry)
        return jobs

    @staticmethod
    def _bottom_threshold(jobs: list[dict[str, Any]]) -> float:
        scores = sorted(job["composite"] for job in jobs)
        index = max(0, int(round(len(scores) * BOTTOM_PERCENT)) - 1)
        return scores[index]

    @staticmethod
    def _reason(
        job: dict[str, Any],
        has_critical: bool,
        qc_issues: list[dict[str, Any]],
    ) -> str:
        parts: list[str] = []
        if has_critical:
            critical = [issue for issue in qc_issues if issue.get("severity") == "critical"]
            parts.append(
                f"存在 {len(critical)} 个 critical QC 问题："
                + "; ".join(str(issue.get("field")) for issue in critical[:3])
            )
        else:
            parts.append(f"综合得分 {job['composite']} 位于实验最低 20%")
        return "；".join(parts) if parts else "实验失败样本"
