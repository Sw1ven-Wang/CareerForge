from typing import Literal
from uuid import UUID

from pydantic import Field

from careerforge.schemas.resources import InputModel


class ExtractedRequirement(InputModel):
    requirement_type: Literal["responsibility", "must", "preferred", "inferred"]
    origin: Literal["explicit", "inferred"]
    normalized_skill: str = Field(min_length=1, max_length=100)
    skill_kind: Literal["tool", "capability"]
    text: str = Field(min_length=1, max_length=4000)
    char_start: int = Field(ge=0, strict=True)
    char_end: int = Field(gt=0, strict=True)
    logical_group: str | None = None


class JobExtraction(InputModel):
    title: str | None
    job_type: Literal["internship", "entry_level", "experienced"] | None
    regions: list[str]
    experience: str | None
    requirements: list[ExtractedRequirement] = Field(max_length=500)
    unresolved: list[str]


class MarketRunInput(InputModel):
    analysis_mode: Literal["offline", "real_agent"] = "offline"
    goal_version_id: UUID
    source_snapshot_ids: list[UUID] = Field(min_length=1, max_length=20)
    retrieval_mode: Literal["import_only"] = "import_only"
    source_queries: list[str] = Field(default_factory=list, max_length=0)
    budget_profile: Literal["development_small"] = "development_small"
    dedup_decision_ids: list[UUID] = Field(default_factory=list, max_length=100)


class RequirementCorrection(ExtractedRequirement):
    expected_version: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=1, max_length=2000)


class DuplicateDecisionInput(InputModel):
    expected_version: int = Field(ge=1, strict=True)
    decision: Literal["duplicate", "distinct", "unknown"]
    reason: str = Field(min_length=1, max_length=2000)
