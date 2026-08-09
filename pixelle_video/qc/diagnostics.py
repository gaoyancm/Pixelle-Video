"""Diagnostic tree for phase 04-B (Q4), modeled on seedance-2.0.

Each issue type maps to a (symptom, root cause, fix) triple so QC reports
say not only what failed but why and how to fix it.
"""

from __future__ import annotations

from typing import Any, Mapping

from pixelle_video.qc.types import QCDiagnosis

_DIAGNOSES: dict[str, tuple[str, str, str]] = {
    "resolution_mismatch": (
        "输出分辨率低于预期",
        "生成节点参数配置错误",
        "检查 workflow JSON 中的 width/height 参数并重新生成",
    ),
    "frame_rate_mismatch": (
        "输出帧率不在预期范围",
        "视频输出节点帧率配置错误",
        "检查视频生成节点的 fps 参数（如 20-30fps）后重新生成",
    ),
    "file_size_abnormal": (
        "输出文件大小超出合理范围",
        "视频时长、码率或压缩设置异常",
        "核对输出时长与码率设置，必要时降低码率或裁剪时长",
    ),
    "audio_missing": (
        "缺少音轨或声道数错误",
        "音频生成/合成节点未正确输出音轨",
        "检查 TTS 与音频合成节点配置，确认声道为单声道或立体声",
    ),
    "frame_instability": (
        "画面抖动或帧间差异过大",
        "镜头运动幅度过大或场景切换过快",
        "降低镜头运动幅度、增加稳定化处理或放缓转场",
    ),
    "character_drift": (
        "角色外貌或服装跨镜头不一致",
        "角色特征提示词在不同镜头间不一致",
        "统一角色 reference 描述，使用一致的身份锚点字段",
    ),
    "subtitle_break_error": (
        "字幕断句错误或缺失",
        "TTS 断句规则与标点处理不当",
        "调整标点与停顿标记，核对字幕时间轴与旁白对齐",
    ),
    "brand_color_deviation": (
        "画面未体现品牌色",
        "提示词或调色流程偏离品牌规范",
        "在提示词中加入品牌色 hex 值并在调色节点校验",
    ),
}


class QCDiagnosticEngine:
    """Return root-cause diagnosis for a QC issue type."""

    def __init__(self, diagnoses: Mapping[str, tuple[str, str, str]] | None = None):
        self._diagnoses = dict(diagnoses) if diagnoses is not None else dict(_DIAGNOSES)

    def known_issue_types(self) -> list[str]:
        return sorted(self._diagnoses)

    def diagnose(self, issue_type: str, context: Mapping[str, Any] | None = None) -> QCDiagnosis:
        entry = self._diagnoses.get(issue_type)
        if entry is None:
            return QCDiagnosis(
                issue_type=issue_type,
                symptom="未知问题类型",
                root_cause="缺少对应诊断规则",
                fix="检查输出与期望规格，人工定位根因",
            )
        symptom, root_cause, fix = entry
        return QCDiagnosis(
            issue_type=issue_type,
            symptom=symptom,
            root_cause=root_cause,
            fix=fix,
        )

    def diagnose_issues(self, issue_fields: list[str]) -> list[dict]:
        """Diagnose a batch of issue fields and return payload-style results."""
        return [
            {
                "issue_type": self.diagnose(field).issue_type,
                "symptom": self.diagnose(field).symptom,
                "root_cause": self.diagnose(field).root_cause,
                "fix": self.diagnose(field).fix,
            }
            for field in issue_fields
        ]
