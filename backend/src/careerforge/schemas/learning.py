from typing import Literal
from uuid import UUID

from pydantic import Field

from careerforge.schemas.resources import InputModel


class LearningInput(InputModel):
    selection_id: UUID
    objective_version_ids: list[UUID] = Field(min_length=1, max_length=1)
    criterion_version_ids: list[UUID] = Field(min_length=1, max_length=50)
    user_prior_knowledge: str = Field(default="", max_length=4000)
    scope_constraints: str = Field(default="", max_length=4000)
    previous_learning_unit_id: UUID | None = None
    feedback_ids: list[UUID] = Field(default_factory=list, max_length=20)
    budget_profile: Literal["development_small"] = "development_small"


class LearningQuestion(InputModel):
    question: str = Field(min_length=1, max_length=4000)
    context_step_id: str | None = None


class ProgressInput(InputModel):
    expected_version: int = Field(ge=1)
    completed_step_ids: list[str] = Field(max_length=30)
    actual_minutes: int | None = Field(default=None, ge=0, le=100000)
    note: str = Field(default="", max_length=4000)


class LearningFeedback(InputModel):
    kind: Literal["citation_error", "explanation_error", "scope_issue"]
    detail: str = Field(min_length=1, max_length=4000)
    citation_ids: list[UUID] = Field(default_factory=list, max_length=30)
