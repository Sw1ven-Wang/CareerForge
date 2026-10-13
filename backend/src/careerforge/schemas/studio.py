from typing import Literal
from uuid import UUID

from pydantic import Field

from careerforge.schemas.builds import ExplanationAnswer
from careerforge.schemas.resources import InputModel


class WorkspaceInput(InputModel):
    name: str = Field(min_length=1, max_length=100)
    files: dict[str, str] = Field(default_factory=dict, max_length=100)


class FileInput(InputModel):
    expected_revision: int = Field(ge=1)
    path: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=100000)


class StudioAgentInput(InputModel):
    expected_revision: int = Field(ge=1)
    instruction: str = Field(min_length=1, max_length=4000)
    objective_version_id: UUID | None = None
    citation_ids: list[UUID] = Field(default_factory=list, max_length=3)
    paths: list[str] = Field(default_factory=list, max_length=3)
    practice_id: UUID | None = None
    mode: Literal["assist", "learn"] = "assist"
    source_snapshot_ids: list[UUID] = Field(default_factory=list, max_length=3)
    knowledge_snapshot_ids: list[UUID] = Field(default_factory=list, max_length=10)
    model_call_limit: int = Field(default=4, ge=2, le=4)


class Edit(InputModel):
    path: str
    before: str | None
    after: str | None
    reason: str


class SuggestedCommand(InputModel):
    argv: list[str] = Field(min_length=1, max_length=30)
    reason: str


class AgentReply(InputModel):
    answer: str = Field(max_length=12000)
    citation_ids: list[str] = Field(max_length=3)
    edits: list[Edit] = Field(max_length=5)
    commands: list[SuggestedCommand] = Field(max_length=3)


class ConfirmInput(InputModel):
    expected_revision: int = Field(ge=1)
    confirmation: Literal["APPLY", "RUN"]


class CommandInput(SuggestedCommand):
    expected_revision: int = Field(ge=1)
    practice_id: UUID | None = None


class PracticeInput(InputModel):
    expected_revision: int = Field(ge=1)
    exercise_index: int = Field(ge=0, le=2)


class PracticeNoteInput(InputModel):
    expected_revision: int = Field(ge=1)
    note: str = Field(max_length=4000)


class ReferenceLinkInput(InputModel):
    objective_version_id: UUID
    repository_snapshot_id: UUID
    path: str = Field(min_length=1, max_length=240)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class LearningPoint(InputModel):
    title: str = Field(min_length=1, max_length=150)
    explanation: str = Field(min_length=1, max_length=1200)
    citation_id: str = Field(min_length=1, max_length=40)
    quote: str = Field(min_length=8, max_length=1000)


class RequirementCodeLink(InputModel):
    source_snapshot_id: str = Field(min_length=1, max_length=40)
    quote: str = Field(min_length=8, max_length=1000)
    citation_id: str = Field(min_length=1, max_length=40)
    explanation: str = Field(min_length=1, max_length=1200)


class LearningGuide(InputModel):
    status: Literal["grounded", "insufficient_evidence"]
    summary: str = Field(min_length=1, max_length=2000)
    requirement_links: list[RequirementCodeLink] = Field(default_factory=list, max_length=4)
    points: list[LearningPoint] = Field(max_length=4)
    reading_order: list[str] = Field(max_length=4)
    practice: list[str] = Field(max_length=3)
    questions: list[str] = Field(max_length=3)
    limitations: list[str] = Field(max_length=4)


class KnowledgeSearchArgs(InputModel):
    query: str = Field(min_length=1, max_length=300)


class KnowledgeChunkArgs(InputModel):
    chunk_id: UUID


class KnowledgeReadArgs(KnowledgeChunkArgs):
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class PracticeSubmissionInput(InputModel):
    expected_revision: int = Field(ge=1)
    summary: str = Field(min_length=1, max_length=8000)
    command_ids: list[UUID] = Field(default_factory=list, max_length=10)
    explanation_answers: list[ExplanationAnswer] = Field(default_factory=list, max_length=30)
    transfer_notes: list[str] = Field(default_factory=list, max_length=30)
    actual_minutes: int | None = Field(default=None, ge=0, le=100000)


class PracticePlanUpdateInput(InputModel):
    expected_plan_version: int = Field(ge=1)
    plan_id: UUID
    base_plan_version_id: UUID
    reason: str = Field(min_length=1, max_length=4000)
    scope_acknowledged: Literal[True]


class WorkspaceSnapshotInput(InputModel):
    expected_revision: int = Field(ge=1)
    files: dict[str, str] = Field(max_length=100)
