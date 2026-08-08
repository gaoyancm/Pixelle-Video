"""Anti-Slop quality diagnostics for phase 04-A (P5).

Diagnostic only: it never blocks template storage. Categories follow the
seedance-2.0 slop blacklist taxonomy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_CATEGORY_LABELS = {
    "superlative_boosters": "Superlative Boosters",
    "quality_assertions": "Quality Assertions",
    "resolution_theater": "Resolution Theater",
    "vague_aesthetic": "Vague Aesthetic",
    "ai_self_praise": "AI Self-Praise",
    "empty_atmosphere": "Empty Atmosphere",
    "redundant_emphasis": "Redundant Emphasis",
    "platform_safety_slop": "Platform Safety Slop",
}

# word (lowercase) -> (category, suggestion)
_SLOP_WORDS: dict[str, tuple[str, str]] = {
    # Superlative Boosters
    "amazing": ("superlative_boosters", "use a concrete, specific descriptor"),
    "incredible": ("superlative_boosters", "describe the actual visual detail"),
    "unbelievable": ("superlative_boosters", "replace with a precise observation"),
    "spectacular": ("superlative_boosters", "describe what makes it striking"),
    # Quality Assertions
    "masterpiece": ("quality_assertions", "state concrete quality attributes"),
    "high quality": ("quality_assertions", "specify resolution and detail level"),
    "best quality": ("quality_assertions", "specify the measurable quality target"),
    "ultra-detailed": ("quality_assertions", "name the details you want visible"),
    # Resolution Theater
    "8k": ("resolution_theater", "use only the resolution the model supports"),
    "4k": ("resolution_theater", "use only the resolution the model supports"),
    "8k uhd": ("resolution_theater", "use the actual supported resolution"),
    "hd": ("resolution_theater", "use explicit pixel dimensions"),
    # Vague Aesthetic
    "beautiful": ("vague_aesthetic", "describe composition, color and light"),
    "stunning": ("vague_aesthetic", "describe the specific visual impact"),
    "gorgeous": ("vague_aesthetic", "replace with concrete aesthetic terms"),
    "striking": ("vague_aesthetic", "explain what draws the eye"),
    # AI Self-Praise
    "ai generated": ("ai_self_praise", "omit self-reference; describe the image"),
    "created by ai": ("ai_self_praise", "omit self-reference; describe the image"),
    "artificial intelligence": ("ai_self_praise", "omit self-reference"),
    # Empty Atmosphere
    "cinematic lighting": ("empty_atmosphere", "name light source, direction, quality"),
    "epic atmosphere": ("empty_atmosphere", "describe the concrete mood and elements"),
    "dramatic atmosphere": ("empty_atmosphere", "describe what creates the drama"),
    "moody atmosphere": ("empty_atmosphere", "name the colors and light that set the mood"),
    # Redundant Emphasis
    "very very": ("redundant_emphasis", "delete the doubled intensifier"),
    "really really": ("redundant_emphasis", "delete the doubled intensifier"),
    "extremely extremely": ("redundant_emphasis", "delete the doubled intensifier"),
    # Platform Safety Slop
    "highly realistic": ("platform_safety_slop", "state concrete realism cues"),
    "photo realistic": ("platform_safety_slop", "state concrete realism cues"),
    "hyper realistic": ("platform_safety_slop", "state concrete realism cues"),
}

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\s-]*[a-z0-9]|[a-z0-9]")


@dataclass(frozen=True)
class SlopViolation:
    word: str
    category: str
    suggestion: str


class AntiSlopChecker:
    """Detect low-signal filler words in prompt text."""

    def check(self, text: str) -> dict[str, Any]:
        """Return a diagnostic report with count, density, and violations."""
        normalized = (text or "").lower()
        word_count = max(1, len(normalized.split()))
        violations: list[SlopViolation] = []
        for phrase, (category, suggestion) in sorted(
            _SLOP_WORDS.items(), key=lambda item: -len(item[0])
        ):
            if phrase in normalized:
                violations.append(
                    SlopViolation(word=phrase, category=category, suggestion=suggestion)
                )
        return {
            "slop_count": len(violations),
            "density": round(len(violations) * 100.0 / word_count, 2),
            "violations": [
                {
                    "word": violation.word,
                    "category": violation.category,
                    "category_label": _CATEGORY_LABELS[violation.category],
                    "suggestion": violation.suggestion,
                }
                for violation in violations
            ],
        }
