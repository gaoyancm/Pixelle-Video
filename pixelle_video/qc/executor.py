"""QC execution engine for phase 04-B (Q2)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Awaitable, Callable, Mapping

from pixelle_video.qc.checkers import ProbeError, evaluate_rule, probe_with_ffprobe
from pixelle_video.qc.models import QCRule
from pixelle_video.qc.repository import (
    QCProfileNotFoundError,
    QCRepository,
)
from pixelle_video.qc.types import QCIssue, QCResult


class QCJobNotFoundError(RuntimeError):
    pass


class QCRunError(RuntimeError):
    pass


async def _default_path_resolver(job_id: str) -> str | None:
    del job_id
    return None


class QCExecutor:
    """Load a profile, run every rule independently, and aggregate issues.

    One rule failing never blocks the others; probe failures degrade to a
    minor issue so diagnostics stay honest without blocking completion.
    """

    def __init__(
        self,
        repository: QCRepository,
        *,
        job_lookup: Callable[[str], Awaitable[object]] | None = None,
        output_path_resolver: Callable[[str], Awaitable[str | None]] | None = None,
        evidence_provider: Callable[[object], Mapping] | None = None,
    ):
        self.repository = repository
        self._job_lookup = job_lookup
        self._output_path_resolver = output_path_resolver or _default_path_resolver
        self._evidence_provider = evidence_provider

    async def run_qc(self, job_id: str, profile_id: str | None = None) -> QCResult:
        profile = None
        if profile_id is not None:
            profile = await self.repository.get_profile(profile_id)
            if profile is None:
                raise QCProfileNotFoundError(f"qc profile not found: {profile_id}")
        else:
            profile = await self.repository.get_default_profile()
            if profile is None:
                raise QCRunError("no default QC profile configured")

        job = None
        if self._job_lookup is not None:
            job = await self._job_lookup(job_id)
            if job is None:
                raise QCJobNotFoundError(f"job not found: {job_id}")

        rules = await self.repository.resolve_profile_rules(profile)

        # Technical evidence: one ffprobe probe reused for all ffprobe rules.
        probe_evidence: dict = {}
        probe_failed: QCIssue | None = None
        path = self._output_path_resolver(job_id)
        if hasattr(path, "__await__"):
            path = await path
        if path is not None and any(rule.provider == "ffprobe" for rule, _ in rules):
            try:
                probe_evidence = await probe_with_ffprobe(path)
            except ProbeError as exc:
                probe_failed = QCIssue(
                    rule_id="ffprobe",
                    severity="minor",
                    category="technical",
                    field="probe",
                    expected=None,
                    actual=None,
                    message=f"ffprobe 无法分析输出文件：{exc}",
                )

        metadata_evidence = self._metadata_evidence(job)
        context_evidence = (
            dict(self._evidence_provider(job))
            if self._evidence_provider and job is not None
            else {}
        )

        issues: list[QCIssue] = []
        for rule, severity_override in rules:
            try:
                evidence = self._evidence_for(
                    rule, probe_evidence, metadata_evidence, context_evidence
                )
                issue = evaluate_rule(
                    rule,
                    evidence,
                    severity_override=severity_override,
                )
                if issue is not None:
                    issues.append(issue)
            except ProbeError as exc:
                issues.append(
                    QCIssue(
                        rule_id=rule.id,
                        severity="minor",
                        category=rule.category,
                        field="probe",
                        expected=None,
                        actual=None,
                        message=f"检查执行失败：{exc}",
                    )
                )
            except Exception as exc:  # one rule failing never blocks the others
                issues.append(
                    QCIssue(
                        rule_id=rule.id,
                        severity="minor",
                        category=rule.category,
                        field=rule.rule_type,
                        expected=None,
                        actual=None,
                        message=f"检查执行异常：{exc}",
                    )
                )
        if probe_failed is not None:
            issues.append(probe_failed)

        return QCResult(
            job_id=job_id,
            executed_at=datetime.now(timezone.utc),
            profile=profile.name,
            total_rules=len(rules),
            issues=issues,
        )

    def _evidence_for(
        self,
        rule: QCRule,
        probe_evidence: Mapping,
        metadata_evidence: Mapping,
        context_evidence: Mapping,
    ) -> dict:
        config = dict(rule.rule_config_json or {})
        field = str(config.get("field") or "")
        if rule.provider == "ffprobe":
            return dict(probe_evidence)
        # Text analysis / parameter checks read from metadata then context.
        evidence = dict(metadata_evidence)
        evidence.update(context_evidence)
        if field not in evidence:
            evidence[field] = None
        return evidence

    @staticmethod
    def _metadata_evidence(job: object | None) -> dict:
        if job is None:
            return {}
        outputs = getattr(job, "output_metadata", None) or []
        first = outputs[0] if outputs else {}
        resolution = None
        if first.get("width") and first.get("height"):
            resolution = f"{first['width']}x{first['height']}"
        return {
            "resolution": resolution,
            "size_bytes": first.get("size"),
            "duration_seconds": first.get("duration"),
            "workflow_type": getattr(job, "workflow_type", None),
        }
