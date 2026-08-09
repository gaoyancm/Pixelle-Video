"""Application service for the phase 04-B QC pipeline."""

from __future__ import annotations

from datetime import datetime, timezone

from pixelle_video.audit import AuditRepository
from pixelle_video.qc.decider import QCDecider
from pixelle_video.qc.diagnostics import QCDiagnosticEngine
from pixelle_video.qc.executor import QCExecutor
from pixelle_video.qc.repository import QCRepository


class QCApplicationService:
    """Own QC business rules and audit persistence."""

    def __init__(
        self,
        repository: QCRepository,
        executor: QCExecutor,
        audit: AuditRepository | None = None,
        decider: QCDecider | None = None,
        diagnostics: QCDiagnosticEngine | None = None,
    ):
        self.repository = repository
        self.executor = executor
        self.audit = audit
        self.decider = decider or QCDecider()
        self.diagnostics = diagnostics or QCDiagnosticEngine()

    # --- Q1 rules / profiles ---------------------------------------------------

    async def create_rule(self, body):
        return await self.repository.create_rule(
            name=body.name,
            category=body.category,
            rule_type=body.rule_type,
            rule_config_json=body.rule_config,
            provider=body.provider,
            priority=body.priority,
        )

    async def list_rules(self, category: str | None, is_active: bool | None):
        return await self.repository.list_rules(category=category, is_active=is_active)

    async def update_rule(self, rule_id: str, body):
        return await self.repository.update_rule(
            rule_id,
            name=body.name,
            category=body.category,
            rule_type=body.rule_type,
            rule_config_json=body.rule_config,
            provider=body.provider,
            is_active=(1 if body.is_active else 0) if body.is_active is not None else None,
            priority=body.priority,
        )

    async def create_profile(self, body):
        return await self.repository.create_profile(
            name=body.name,
            rules_json=body.rules,
            description=body.description,
            is_default=1 if body.is_default else 0,
        )

    async def list_profiles(self):
        return await self.repository.list_profiles()

    # --- Q2 execution ------------------------------------------------------------

    async def run_qc(self, job_id: str, profile_id: str | None = None) -> dict:
        result = await self.executor.run_qc(job_id, profile_id=profile_id)
        decision = self.decider.decide(result)
        payload = result.to_payload()
        payload["decision"] = decision.decision
        payload["suggestions"] = self._suggestions(result)
        if self.audit is not None:
            try:
                await self.audit.record(
                    event_type="qc_completed",
                    scope_type="job",
                    scope_id=job_id,
                    details={
                        "profile": result.profile,
                        "total_rules": result.total_rules,
                        "passed": result.passed,
                        "issues": [
                            {
                                "rule_id": issue.rule_id,
                                "severity": issue.severity,
                                "category": issue.category,
                                "field": issue.field,
                                "expected": issue.expected,
                                "actual": issue.actual,
                                "message": issue.message,
                            }
                            for issue in result.issues
                        ],
                        "decision": decision.decision,
                        "summary": decision.summary,
                    },
                )
            except Exception:
                pass
        return payload

    async def get_result(self, job_id: str) -> dict | None:
        if self.audit is None:
            return None
        events, _ = await self.audit.list(
            scope_type="job", scope_id=job_id, event_type="qc_completed"
        )
        if not events:
            return None
        latest = events[-1]
        details = dict(latest.details_json or {})
        details.pop("summary", None)
        fields = [issue.get("field") for issue in details.get("issues", [])]
        details["job_id"] = job_id
        details["executed_at"] = latest.created_at
        details["suggestions"] = [
            diagnosis.fix for diagnosis in (self.diagnostics.diagnose(field) for field in fields)
        ]
        return details

    def _suggestions(self, result) -> list[str]:
        fields = [issue.field for issue in result.issues]
        return [
            diagnosis.fix for diagnosis in (self.diagnostics.diagnose(field) for field in fields)
        ]

    # --- Q4 diagnostics ------------------------------------------------------------

    async def diagnose(self, issue_type: str, context: dict | None = None) -> dict:
        diagnosis = self.diagnostics.diagnose(issue_type, context)
        return {
            "issue_type": diagnosis.issue_type,
            "symptom": diagnosis.symptom,
            "root_cause": diagnosis.root_cause,
            "fix": diagnosis.fix,
        }

    async def report(self, job_id: str) -> dict:
        result = await self.executor.run_qc(job_id)
        decision = self.decider.decide(result)
        fields = [issue.field for issue in result.issues]
        diagnostics = [
            {
                "issue_type": diagnosis.issue_type,
                "symptom": diagnosis.symptom,
                "root_cause": diagnosis.root_cause,
                "fix": diagnosis.fix,
            }
            for diagnosis in (self.diagnostics.diagnose(field) for field in fields)
        ]
        return {
            "job_id": job_id,
            "profile": result.profile,
            "decision": decision.decision,
            "summary": decision.summary,
            "passed": result.passed,
            "total_rules": result.total_rules,
            "issues": [
                {
                    "rule_id": issue.rule_id,
                    "severity": issue.severity,
                    "category": issue.category,
                    "field": issue.field,
                    "expected": issue.expected,
                    "actual": issue.actual,
                    "message": issue.message,
                }
                for issue in result.issues
            ],
            "diagnostics": diagnostics,
            "report_text": self._render_text(job_id, result, decision, diagnostics),
        }

    @staticmethod
    def _render_text(job_id: str, result, decision, diagnostics: list[dict]) -> str:
        lines = [
            f"QC 报告 — 任务 {job_id}",
            f"检查方案：{result.profile}",
            f"通过：{result.passed}/{result.total_rules}",
            f"决策：{decision.decision}（{decision.summary}）",
            "",
            "问题清单：",
        ]
        for issue in result.issues:
            lines.append(f"- [{issue.severity}] {issue.category}/{issue.field}: {issue.message}")
        if diagnostics:
            lines.append("")
            lines.append("诊断与修复建议：")
            for diagnosis in diagnostics:
                lines.append(
                    f"- {diagnosis['issue_type']}: {diagnosis['root_cause']} → {diagnosis['fix']}"
                )
        lines.append("")
        lines.append(f"生成时间：{datetime.now(timezone.utc).isoformat()}")
        return "\n".join(lines)
