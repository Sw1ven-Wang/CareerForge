"""Explicit one-level review tasks; models cannot enlarge the root's budget or scope."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from careerforge.schemas.resources import InputModel


class ReviewBatchInput(InputModel):
    expected_revision: int = Field(ge=1)
    candidate_ids: list[UUID] = Field(min_length=1, max_length=2)
    per_child_call_limit: int = Field(default=1, ge=1, le=3)
    call_budget: int = Field(ge=1, le=6)
    max_output_tokens: int = Field(default=8192, ge=512, le=8192)

    @model_validator(mode="after")
    def valid_budget(self) -> Self:
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("候选不可重复")
        if self.call_budget < len(self.candidate_ids) * self.per_child_call_limit:
            raise ValueError("总额度不足以预留所选子任务")
        return self


class ReviewTaskContract(InputModel):
    parent_run_id: UUID
    requirements_id: UUID
    candidate_id: UUID
    candidate_hash: str
    requirement_keys: list[str]
    source_snapshot_ids: list[str]
    context_refs: list[str]
    role: Literal["review"] = "review"
    allowed_tools: list[str]
    call_limit: int = Field(ge=1, le=3)
    max_output_tokens: int = Field(ge=512, le=8192)
    deadline: str
    output_schema: Literal["ReviewOutput+CodeInvestigation"] = "ReviewOutput+CodeInvestigation"
    stop_conditions: list[str]
