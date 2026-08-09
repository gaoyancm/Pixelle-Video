"""QC decision rules for phase 04-B (Q3).

Four decisions follow V1.0 Section 10.3: pass / human_review / partial_redo /
full_reject, plus an explicit pass_with_warnings for minor-only findings.
"""

from __future__ import annotations

from pixelle_video.qc.types import QCDecision, QCResult


class QCDecider:
    """Map a QCResult into one of the five decision outcomes."""

    def decide(self, result: QCResult) -> QCDecision:
        issues = result.issues
        critical = [issue for issue in issues if issue.severity == "critical"]
        major = [issue for issue in issues if issue.severity == "major"]
        minor = [issue for issue in issues if issue.severity == "minor"]

        if critical:
            if len(critical) >= 2:
                return QCDecision(
                    decision="full_reject",
                    summary=(
                        f"发现 {len(critical)} 个 critical 问题，属于系统性质量缺陷，"
                        "建议整体退回重新策划"
                    ),
                )
            return QCDecision(
                decision="partial_redo",
                summary=(f"发现 {len(critical)} 个 critical 问题，建议仅对相关镜头/资产局部重做"),
            )

        if len(major) >= 2:
            return QCDecision(
                decision="full_reject",
                summary=f"发现 {len(major)} 个 major 问题，建议整体退回",
            )

        if major:
            return QCDecision(
                decision="human_review",
                summary="发现 major 问题但无 critical，建议人工复核",
            )

        if minor:
            return QCDecision(
                decision="pass_with_warnings",
                summary=f"存在 {len(minor)} 个 minor 警告，可通过",
            )

        return QCDecision(decision="pass", summary="所有 QC 检查通过")
