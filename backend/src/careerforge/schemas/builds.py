from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from careerforge.schemas.resources import InputModel


class ProjectInput(InputModel):
    title: str = Field(min_length=1, max_length=300)
    problem_statement: str = Field(min_length=1, max_length=8000)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    evidence_refs: list[UUID] = Field(default_factory=list, max_length=20)
    public_repository_url: str | None = Field(default=None, max_length=500)


class TaskInput(InputModel):
    objective_version_id: UUID
    criterion_version_ids: list[UUID] = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=8000)
    deliverables: list[str] = Field(min_length=1, max_length=30)
    prerequisite_task_ids: list[UUID] = Field(default_factory=list, max_length=30)
    learning_unit_id: UUID | None = None
    task_draft_id: UUID | None = None

    @model_validator(mode="after")
    def paired(self) -> "TaskInput":
        if bool(self.learning_unit_id) != bool(self.task_draft_id):
            raise ValueError("草稿和学习单元必须同时提供")
        return self


class TaskRevision(TaskInput):
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=1, max_length=4000)


class TransitionInput(InputModel):
    expected_version: int = Field(ge=1)
    target_status: Literal["in_progress", "cancelled"]
    reason: str = Field(min_length=1, max_length=4000)


class ExecutionRecord(InputModel):
    command_text: str = Field(min_length=1, max_length=4000)
    reported_exit_code: int | None = None
    output_attachment_id: UUID
    environment_refs: list[str] = Field(min_length=1, max_length=30)
    reported_at: str = Field(min_length=1, max_length=100)
    execution_provenance: Literal["user_reported"] = "user_reported"


class ExplanationAnswer(InputModel):
    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(min_length=1, max_length=12000)
    criterion_version_ids: list[UUID] = Field(min_length=1, max_length=50)


class SubmissionInput(InputModel):
    expected_version: int = Field(ge=1)
    task_version_id: UUID
    criterion_version_ids: list[UUID] = Field(min_length=1, max_length=50)
    summary: str = Field(min_length=1, max_length=8000)
    code_commit: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{40}$")
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=20)
    environment: dict[str, str] = Field(default_factory=dict, max_length=30)
    execution_records: list[ExecutionRecord] = Field(default_factory=list, max_length=30)
    explanation_answers: list[ExplanationAnswer] = Field(default_factory=list, max_length=30)
    transfer_notes: list[str] = Field(default_factory=list, max_length=30)
    actual_minutes: int | None = Field(default=None, ge=0, le=100000)


class AssessmentInput(InputModel):
    criterion_version_ids: list[UUID] = Field(min_length=1, max_length=50)
    review_scope: str = Field(default="完整条件；材料不足保持未知", max_length=4000)
    budget_profile: Literal["development_small"] = "development_small"


class CriterionDecision(InputModel):
    criterion_version_id: UUID
    verdict: Literal["pass", "fail", "unknown"]
    method: Literal["model_review", "user_reported", "human_review"]
    evidence_refs: list[UUID] = Field(default_factory=list, max_length=30)
    reason: str = Field(min_length=1, max_length=4000)
    self_review_record: dict[str, str] | None = Field(default=None, max_length=10)


class ConfirmationInput(InputModel):
    expected_version: int = Field(ge=1)
    expected_task_version: int = Field(ge=1)
    decision: Literal["accept", "request_revision"]
    criterion_decisions: list[CriterionDecision] = Field(min_length=1, max_length=50)
    supplementary_evidence_ids: list[UUID] = Field(default_factory=list, max_length=20)
    scope_acknowledged: Literal[True]


class RevocationInput(InputModel):
    expected_version: int = Field(ge=1)
    expected_task_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=4000)
