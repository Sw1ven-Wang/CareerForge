from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from careerforge.schemas.resources import InputModel


class EvidenceInput(InputModel):
    kind: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=20000)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=20)
    public_repository_snapshot_id: UUID | None = None
    code_location: dict[str, Any] | None = None
    claimed_skills: list[str] = Field(default_factory=list, max_length=50)
    declared_verification: Literal["self_reported", "attached_material"] = "self_reported"
    environment: dict[str, str] = Field(default_factory=dict)


class EvidenceRevision(EvidenceInput):
    expected_version: int = Field(ge=1, strict=True)
    change_reason: str = Field(min_length=1, max_length=2000)


class Effort(InputModel):
    min: float = Field(ge=0, le=1000)
    max: float = Field(gt=0, le=1000)
    is_estimate: Literal[True] = True

    @model_validator(mode="after")
    def ordered(self) -> "Effort":
        if self.min > self.max:
            raise ValueError("工时区间倒置")
        return self


class CriterionInput(InputModel):
    dimension: Literal["artifact", "explanation", "transfer"]
    scenario: str = Field(min_length=1, max_length=4000)
    expected_behavior: str = Field(min_length=1, max_length=4000)
    pass_rule: str = Field(min_length=1, max_length=4000)
    fail_rule: str = Field(min_length=1, max_length=4000)
    unknown_rule: str = Field(min_length=1, max_length=4000)
    critical: bool = True
    allowed_methods: list[Literal["model_review", "user_reported", "human_review"]] = Field(
        min_length=1, max_length=3
    )
    required_artifact_types: list[str] = Field(min_length=1, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class ObjectiveInput(InputModel):
    goal_version_id: UUID
    title: str = Field(min_length=1, max_length=500)
    target_behavior: str = Field(min_length=1, max_length=4000)
    scope: str = Field(min_length=1, max_length=4000)
    prerequisite_objective_ids: list[UUID] = Field(default_factory=list, max_length=20)
    effort_hours: Effort | None = None
    deliverables: list[str] = Field(min_length=1, max_length=20)
    criteria: list[CriterionInput] = Field(min_length=1, max_length=30)
    scope_confirmed: Literal[True]


class ObjectiveRevision(ObjectiveInput):
    expected_version: int = Field(ge=1, strict=True)
    change_reason: str = Field(min_length=1, max_length=2000)


class UserConstraints(InputModel):
    available_hours: float | None = Field(default=None, gt=0, le=10000)
    notes: str = Field(default="", max_length=4000)


class AlignmentInput(InputModel):
    goal_version_id: UUID
    market_report_id: UUID
    personal_evidence_version_ids: list[UUID] = Field(default_factory=list, max_length=20)
    previous_plan_version_id: UUID | None = None
    assessment_ids: list[UUID] = Field(default_factory=list, max_length=20)
    user_constraints: UserConstraints = Field(default_factory=UserConstraints)
    budget_profile: Literal["development_small"] = "development_small"


class PlanRevision(InputModel):
    expected_version: int = Field(ge=1, strict=True)
    base_plan_version_id: UUID
    objective_version_ids: list[UUID] = Field(min_length=1, max_length=30)
    adjustment_reason: str = Field(min_length=1, max_length=4000)
    deferred: list[str] = Field(default_factory=list, max_length=30)
    user_constraints: UserConstraints = Field(default_factory=UserConstraints)


class PlanAdoption(InputModel):
    expected_version: int = Field(ge=1, strict=True)
    plan_version_id: UUID
    expected_goal_version_id: UUID
    selected_objective_version_ids: list[UUID] = Field(min_length=1, max_length=30)


class PlanObjectiveRevision(InputModel):
    expected_plan_version: int = Field(ge=1, strict=True)
    base_plan_version_id: UUID
    objective: ObjectiveRevision
