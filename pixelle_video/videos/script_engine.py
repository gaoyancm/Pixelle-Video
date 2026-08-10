"""Phase 06 S1: topic-to-script engine.

Compiles a structured short-video script (hook + scenes + emotion curve +
total duration) through the phase 04-A prompt compiler, then persists it
as a video_scripts row. No pipeline logic is modified.
"""

from __future__ import annotations

from typing import Any, Callable

from pixelle_video.videos.repository import VideoScriptRepository

SCRIPT_TEMPLATE = (
    "为短视频主题「{{topic}}」编写一条 {{duration}} 秒的完整脚本。\n\n"
    "语言：{{language}}；目标平台：{{platform}}。\n\n"
    "要求：\n"
    "1. 开场 Hook 3-5 秒抓住注意力\n"
    "2. 正文按 2-3 个分段递进\n"
    "3. 结尾 CTA 行动号召\n"
    "4. 每段标注画面方向 visual_direction\n"
    "5. 总时长与目标一致\n\n"
    "脚本："
)


class ScriptEngine:
    """Turn a topic sentence into a structured, prompt-versioned script."""

    def __init__(
        self,
        repository: VideoScriptRepository,
        *,
        prompt_compiler: Callable[..., str] | None = None,
    ):
        self.repository = repository
        self.prompt_compiler = prompt_compiler

    async def generate_script(
        self,
        *,
        topic: str,
        language: str = "zh-CN",
        target_duration: int = 60,
        platform: str = "tiktok",
        project_id: str | None = None,
        script_id: str | None = None,
    ) -> dict[str, Any]:
        """Generate, persist, and return a structured script."""
        script_json = self._build_script(
            topic=topic,
            language=language,
            target_duration=target_duration,
            platform=platform,
        )
        script = await self.repository.create_script(
            topic=topic,
            language=language,
            target_duration=target_duration,
            platform=platform,
            project_id=project_id,
            script_json=script_json,
            prompt_version_id="pt-short-video-script",
            script_id=script_id,
        )
        return {
            "id": script.id,
            "topic": script.topic,
            "language": script.language,
            "target_duration": script.target_duration,
            "platform": script.platform,
            "status": script.status,
            "script_json": script.script_json,
            "prompt_version_id": script.prompt_version_id,
        }

    def _build_script(
        self, *, topic: str, language: str, target_duration: int, platform: str
    ) -> dict[str, Any]:
        compiled = ""
        if self.prompt_compiler is not None:
            compiled = self.prompt_compiler(
                SCRIPT_TEMPLATE,
                {
                    "topic": topic,
                    "duration": str(target_duration),
                    "language": language,
                    "platform": platform,
                },
            )
        return self._structure(compiled, topic, target_duration)

    @staticmethod
    def _structure(compiled: str, topic: str, target_duration: int) -> dict[str, Any]:
        """Build the contract's script_json shape from the compiled text."""
        hook_text = (
            compiled.strip().split("\n")[0] if compiled.strip() else (f"3 秒抓住注意力：{topic}")
        )
        body_text = (
            compiled.strip()
            if compiled.strip()
            else (f"围绕 {topic} 展开 2-3 个递进段落，配合画面与旁白推进信息")
        )
        body_duration = max(target_duration - 13, 10)
        scenes = [
            {
                "type": "opening",
                "text": hook_text[:120],
                "duration": 5,
                "visual_direction": "强视觉冲击开场，快速切近景",
            },
            {
                "type": "body",
                "text": body_text[:300],
                "duration": body_duration,
                "visual_direction": "信息递进，中景与特写交替",
            },
            {
                "type": "closing",
                "text": f"点赞关注，获取更多{topic}相关内容",
                "duration": 8,
                "visual_direction": "CTA 结尾，画面定格品牌/主题",
            },
        ]
        return {
            "hook": scenes[0]["text"],
            "scenes": scenes,
            "emotion_curve": "开场震撼→信息递进→结尾行动号召",
            "total_duration": target_duration,
        }
