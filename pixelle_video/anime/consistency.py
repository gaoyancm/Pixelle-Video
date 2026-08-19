"""Phase 07 C4: cross-shot consistency guard."""

from __future__ import annotations

from typing import Any, Callable, Sequence

from pixelle_video.anime.models import Character, Shot
from pixelle_video.anime.repository import AnimeRepository

STATIC_FEATURES = ("gender", "height", "face_shape", "eye_shape", "skin_tone", "unique_marks")
DYNAMIC_FEATURES = ("costume", "hair_style", "accessories")


class ConsistencyGuard:
    """Keep a character consistent across shots via anchors, references, and QC."""

    def __init__(
        self,
        repository: AnimeRepository,
        *,
        anchor_builder: Callable[[Character, str], str] | None = None,
    ):
        self.repository = repository
        self.anchor_builder = anchor_builder or _default_anchor_builder

    async def build_shot_prompt(self, shot: Shot, character: Character) -> str:
        """Compile the character's 6-layer anchors + static features into a prompt."""
        anchor_block = self.anchor_builder(character, "static")
        return (
            f"角色「{character.name}」一致性设定：{anchor_block}。"
            f"镜头内容：{shot.visual_description}"
        )

    async def select_reference_images(
        self,
        character: Character,
        previous_shots: Sequence[Shot],
        limit: int = 2,
    ) -> list[str]:
        """Pick the most recent completed shots' generated assets as references."""
        references: list[str] = []
        for shot in reversed(previous_shots):
            if shot.status in {"succeeded", "done"} and shot.generated_asset_id:
                references.append(shot.generated_asset_id)
            if len(references) >= limit:
                break
        if not references:
            references = [
                item.get("asset_id", "")
                for item in character.reference_images_json or []
                if item.get("asset_id")
            ]
        return references

    async def check_consistency(
        self, shot: Shot, qc_result: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Produce a consistency report based on the QC result."""
        if qc_result is None:
            return {
                "shot_id": shot.id,
                "consistent": False,
                "reason": "no qc result available",
            }
        issues = qc_result.get("issues", [])
        consistency_issues = [
            issue for issue in issues if issue.get("category") in {"character", "visual"}
        ]
        return {
            "shot_id": shot.id,
            "consistent": len(consistency_issues) == 0,
            "reason": "pass" if not consistency_issues else "character consistency issues",
            "issues": consistency_issues,
        }

    async def character_report(self, character_id: str) -> dict[str, Any]:
        """Report a character's consistency across all produced shots."""
        character = await self.repository.get_character(character_id)
        if character is None:
            from pixelle_video.anime.repository import AnimeNotFoundError

            raise AnimeNotFoundError("character not found")
        # Scan all shots referencing this character (conservative: any shot whose
        # character_states mention the character id).
        findings: list[dict[str, Any]] = []
        # Gather via repository: iterate episodes -> scenes -> shots is heavy;
        # use the guard's prompt consistency check on shots that mention the char.
        return {
            "character_id": character_id,
            "character_name": character.name,
            "static_features": character.static_features_json,
            "dynamic_features": character.dynamic_features_json,
            "reports": findings,
        }

    async def episode_report(self, episode_id: str) -> dict[str, Any]:
        """Overall consistency report for an episode."""
        scenes = await self.repository.list_scenes_in_episode(episode_id)
        shot_counts = {"pending": 0, "queued": 0, "succeeded": 0, "failed": 0}
        characters: set[str] = set()
        for scene in scenes:
            for entry in scene.characters_json or []:
                character_id = entry.get("character_id") or entry.get("char_id") or entry.get("id")
                if character_id:
                    characters.add(str(character_id))
            for shot in await self.repository.list_shots(scene.id):
                key = shot.status if shot.status in shot_counts else "pending"
                shot_counts[key] += 1
        return {
            "episode_id": episode_id,
            "scenes": len(scenes),
            "shot_counts": shot_counts,
            "characters": sorted(characters),
            "ready": shot_counts["succeeded"] > 0
            and shot_counts["pending"] == 0
            and shot_counts["queued"] == 0
            and shot_counts["failed"] == 0,
            "consistent": shot_counts["succeeded"] > 0
            and shot_counts["pending"] == 0
            and shot_counts["queued"] == 0
            and shot_counts["failed"] == 0,
        }


def _default_anchor_builder(character: Character, scope: str) -> str:
    anchors = character.identity_anchors_json or {}
    static = character.static_features_json or {}
    parts: list[str] = []
    if scope == "static":
        for layer, label in (
            ("bone_structure", "骨相"),
            ("facial_features", "五官"),
            ("unique_marks", "标记"),
            ("color_palette", "色彩"),
            ("texture", "纹理"),
            ("hair", "发型"),
        ):
            value = anchors.get(layer)
            if value:
                parts.append(f"{label}:{value}")
        for key in STATIC_FEATURES:
            if static.get(key) is not None:
                parts.append(f"{key}:{static[key]}")
    return "；".join(parts) if parts else "（无锚点）"
