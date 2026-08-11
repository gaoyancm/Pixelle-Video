"""Pydantic output contracts for the phase 04-E sub-agents."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Grade = Literal["A", "B", "C", "D"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubAgentResult(StrictModel):
    """Every sub-agent returns structured content plus usage accounting."""

    prompt: str = Field(max_length=20_000)
    content: dict[str, Any]
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    cost: float = Field(ge=0)

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


class StrategistOutput(StrictModel):
    target_audience: str
    creative_directions: list[dict[str, str]]
    visual_style: dict[str, str]


class CopywriterOutput(StrictModel):
    hooks: list[str]
    ctas: list[str]
    body_copy: str


class StoryboardOutput(StrictModel):
    scenes: list[dict[str, Any]]
    camera_notes: str


class EpisodePlanOutput(StrictModel):
    seasons: list[dict[str, Any]]
    character_arcs: list[dict[str, Any]] = Field(default_factory=list)
    foreshadowing_map: list[dict[str, Any]] = Field(default_factory=list)


class ConsistencyVerdictOutput(StrictModel):
    consistent: bool
    issues: list[str] = Field(default_factory=list)


class SupervisionOutput(StrictModel):
    grade: Grade
    severe_issues: int = Field(ge=0)
    medium_issues: int = Field(ge=0)
    suggestions: list[str] = Field(default_factory=list)
