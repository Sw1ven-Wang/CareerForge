"""Server-bounded autonomous delegation. Tools and authority are never model inputs."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from careerforge.schemas.resources import InputModel

TaskRole = Literal[
    "requirement_analysis", "code_investigation", "evidence_review", "learning_design"
]
ShortText = Annotated[str, Field(min_length=1, max_length=500)]


def default_roles() -> list[TaskRole]:
    return ["requirement_analysis", "code_investigation", "evidence_review", "learning_design"]


class DelegationInput(InputModel):
    expected_revision: int = Field(ge=1)
    objective: str = Field(min_length=1, max_length=3000)
    requirement_keys: list[str] = Field(min_length=1, max_length=8)
    candidate_ids: list[UUID] = Field(default_factory=list, max_length=2)
    allowed_roles: list[TaskRole] = Field(
        default_factory=default_roles,
        min_length=1,
        max_length=4,
    )
    call_budget: int = Field(default=6, ge=3, le=10)
    parent_call_limit: int = Field(default=3, ge=2, le=4)
    max_output_tokens: int = Field(default=8192, ge=512, le=8192)

    @model_validator(mode="after")
    def distinct(self) -> Self:
        for values in (self.requirement_keys, self.candidate_ids, self.allowed_roles):
            if len(values) != len(set(values)):
                raise ValueError("范围或角色不可重复")
        if self.call_budget < self.parent_call_limit:
            raise ValueError("总额度不能小于父任务调用上限")
        return self


class DelegateArgs(InputModel):
    task_key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    role: TaskRole
    objective: str = Field(min_length=1, max_length=1500)
    requirement_keys: list[str] = Field(min_length=1, max_length=8)
    candidate_id: UUID | None = None
    call_limit: int = Field(default=1, ge=1, le=3)


class TaskEvidence(InputModel):
    ref_id: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=8, max_length=600)


class TaskFinding(InputModel):
    statement: str = Field(min_length=1, max_length=800)
    evidence: list[TaskEvidence] = Field(min_length=1, max_length=3)


class TaskOutput(InputModel):
    summary: str = Field(min_length=1, max_length=1000)
    findings: list[TaskFinding] = Field(default_factory=list, max_length=4)
    unresolved: list[ShortText] = Field(default_factory=list, max_length=5)
    counterevidence: list[ShortText] = Field(default_factory=list, max_length=5)
    stop_reason: Literal["evidence_sufficient", "unsupported", "budget"]


class DelegationClaim(InputModel):
    statement: str = Field(min_length=1, max_length=800)
    evidence_ids: list[str] = Field(min_length=1, max_length=4)


class DelegationOutput(InputModel):
    summary: str = Field(min_length=1, max_length=2000)
    claims: list[DelegationClaim] = Field(default_factory=list, max_length=6)
    unresolved: list[ShortText] = Field(default_factory=list, max_length=8)
    counterevidence: list[ShortText] = Field(default_factory=list, max_length=6)
    stop_reason: Literal["completed", "insufficient_evidence", "budget"]


class DelegatedTaskContract(InputModel):
    parent_run_id: UUID
    proposal: DelegateArgs
    requirement_version: UUID
    snapshot_hash: str | None
    source_refs: list[str]
    max_output_tokens: int = Field(ge=512, le=8192)
    allowed_tools: list[str]
    context_refs: list[str]
    deadline: str
    max_depth: Literal[0] = 0
    call_limit: int = Field(ge=1, le=3)
    output_schema: Literal["TaskOutput"] = "TaskOutput"
    stop_conditions: list[str]
