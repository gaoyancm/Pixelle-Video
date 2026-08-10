"""Phase 06 S3: voice-over, subtitle, BGM, and composition."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from pixelle_video.videos.repository import VideoScriptRepository

SRT_TEMPLATE = "{index}\n{start} --> {end}\n{text}\n"


def _ts(seconds: float) -> str:
    milliseconds = int(round((seconds - int(seconds)) * 1000))
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def build_srt(scenes: list[dict[str, Any]]) -> str:
    """Deterministic line-level subtitles from the script scenes."""
    lines: list[str] = []
    cursor = 0.0
    for index, scene in enumerate(scenes, start=1):
        duration = float(scene.get("duration", 5))
        text = (scene.get("text") or "").strip()
        if not text:
            cursor += duration
            continue
        lines.append(
            SRT_TEMPLATE.format(
                index=index,
                start=_ts(cursor),
                end=_ts(cursor + duration),
                text=text,
            )
        )
        cursor += duration
    return "\n".join(lines).strip()


def build_vtt(srt: str) -> str:
    """Convert SRT text to VTT."""
    return "WEBVTT\n\n" + srt.replace(",", ".")


def narration_text(scenes: list[dict[str, Any]]) -> str:
    return " ".join((scene.get("text") or "").strip() for scene in scenes)


def emotion_profile(scenes: list[dict[str, Any]]) -> str:
    opening = (scenes[0].get("type") if scenes else "") or ""
    if opening == "opening":
        return (
            "激昂"
            if any(
                word in (scene.get("text") or "")
                for scene in scenes
                for word in ("!", "！", "震撼")
            )
            else "舒缓"
        )
    return "科技感"


class Composer:
    """Assemble frames + voice-over + subtitles + BGM into a single MP4."""

    def __init__(
        self,
        script_repository: VideoScriptRepository,
        *,
        tts_runner: Callable[[str], Awaitable[str]] | None = None,
        bgm_matcher: Callable[[str], Awaitable[str]] | None = None,
        ffmpeg: str = "ffmpeg",
        work_dir: str | None = None,
    ):
        self.script_repository = script_repository
        self.tts_runner = tts_runner
        self.bgm_matcher = bgm_matcher
        self.ffmpeg = ffmpeg
        self.work_dir = Path(work_dir) if work_dir else Path(".") / ".video-work"

    async def compose(self, script_id: str) -> dict[str, Any]:
        """Synthesize voice-over, subtitles, BGM and the final MP4."""
        script = await self._require(script_id)
        scenes = (script.script_json or {}).get("scenes", [])
        frames = (script.script_json or {}).get("storyboard", {}).get("frames", [])
        output_dir = self.work_dir / script_id
        output_dir.mkdir(parents=True, exist_ok=True)

        # 1. Frames: use real generated assets when available, else placeholders.
        video_list = output_dir / "frames.txt"
        segments = await self._segment_lines(frames, scenes, output_dir)
        video_list.write_text("\n".join(segments), encoding="utf-8")

        # 2. Voice-over via the injected TTS runner.
        audio_path = None
        if self.tts_runner is not None:
            text = narration_text(scenes)
            if text:
                audio_path = await self.tts_runner(text)

        # 3. Subtitles.
        srt_path = output_dir / "subtitle.srt"
        srt_path.write_text(build_srt(scenes), encoding="utf-8")
        vtt_path = output_dir / "subtitle.vtt"
        vtt_path.write_text(build_vtt(srt_path.read_text(encoding="utf-8")), encoding="utf-8")

        # 4. BGM matched from the emotion curve.
        bgm_path = None
        if self.bgm_matcher is not None:
            emotion = emotion_profile(scenes)
            bgm_path = await self.bgm_matcher(emotion)

        # 5. Compose with ffmpeg: concat video + voice-over + burned subtitles + BGM.
        output = output_dir / "final.mp4"
        await self._run_ffmpeg(video_list, audio_path, srt_path, bgm_path, output)
        await self.script_repository.update_status(script_id, "completed")
        return {
            "script_id": script_id,
            "status": "completed",
            "video": str(output),
            "srt": str(srt_path),
            "vtt": str(vtt_path),
            "audio": audio_path,
            "bgm": bgm_path,
        }

    async def compose_status(self, script_id: str) -> dict[str, Any]:
        script = await self._require(script_id)
        output = self.work_dir / script_id / "final.mp4"
        return {
            "script_id": script_id,
            "status": script.status,
            "completed": output.exists(),
            "video": str(output) if output.exists() else None,
        }

    def result_path(self, script_id: str) -> str | None:
        output = self.work_dir / script_id / "final.mp4"
        return str(output) if output.exists() else None

    # --- helpers ------------------------------------------------------------------

    async def _segment_lines(
        self,
        frames: list[dict[str, Any]],
        scenes: list[dict[str, Any]],
        output_dir: Path,
    ) -> list[str]:
        lines: list[str] = []
        if frames:
            for frame in frames:
                duration = float(frame.get("duration", 5))
                asset_id = frame.get("generated_asset_id")
                job_asset = None
                if asset_id:
                    job_asset = (
                        getattr(self, "_asset_paths", {})
                        .get(str(frame.get("index")), {})
                        .get(asset_id)
                    )
                if job_asset and Path(job_asset).exists():
                    lines.append(f"file '{job_asset}'")
                    lines.append(f"duration {duration}")
                else:
                    placeholder = output_dir / f"placeholder_{frame['index']}.mp4"
                    await self._make_placeholder(placeholder, duration)
                    lines.append(f"file '{placeholder}'")
                    lines.append(f"duration {duration}")
        elif scenes:
            for index, scene in enumerate(scenes, start=1):
                duration = float(scene.get("duration", 5))
                placeholder = output_dir / f"placeholder_{index}.mp4"
                await self._make_placeholder(placeholder, duration)
                lines.append(f"file '{placeholder}'")
                lines.append(f"duration {duration}")
        return lines

    async def _make_placeholder(self, path: Path, duration: float) -> None:
        if path.exists():
            return
        completed = await _run(
            [
                self.ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x2b6cb0:s=1280x720:d={duration}:r=15",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ]
        )
        if completed != 0:
            raise RuntimeError(f"placeholder generation failed: {path.name}")

    async def _run_ffmpeg(
        self,
        video_list: Path,
        audio_path: str | None,
        srt_path: Path,
        bgm_path: str | None,
        output: Path,
    ) -> None:
        # Concatenate the frame segments first.
        concat = video_list.parent / "concat.mp4"
        await _run(
            [
                self.ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(video_list),
                "-c",
                "copy",
                str(concat),
            ]
        )
        # Add subtitles + audio.
        command = [
            self.ffmpeg,
            "-y",
            "-i",
            str(concat),
        ]
        filter_complex: list[str] = []
        if audio_path:
            command += ["-i", str(audio_path)]
            if bgm_path:
                command += ["-i", str(bgm_path)]
                filter_complex.append(
                    "[1:a]volume=1.0[voice];[2:a]volume=0.3[bgm];[voice][bgm]amix=inputs=2:duration=first[outa]"
                )
                command += [
                    "-filter_complex",
                    ";".join(filter_complex),
                    "-map",
                    "0:v",
                    "-map",
                    "[outa]",
                ]
            else:
                command += ["-map", "0:v", "-map", "1:a", "-c:a", "aac"]
        else:
            if bgm_path:
                command += ["-i", str(bgm_path)]
                command += ["-map", "0:v", "-map", "1:a", "-c:a", "aac"]
            else:
                command += ["-map", "0:v"]
        srt_filter_path = str(srt_path).replace(chr(92), "/").replace(":", "\\:")
        subtitles_filter = f"subtitles='{srt_filter_path}'"
        command += ["-vf", subtitles_filter, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output)]
        completed = await _run(command)
        if completed != 0:
            raise RuntimeError(f"ffmpeg composition failed for {output.name}")

    async def _require(self, script_id: str):
        script = await self.script_repository.get_script(script_id)
        if script is None:
            from pixelle_video.videos.repository import VideoScriptNotFoundError

            raise VideoScriptNotFoundError("video script not found")
        return script


async def _run(command: list[str]) -> int:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()
    return process.returncode or 0
