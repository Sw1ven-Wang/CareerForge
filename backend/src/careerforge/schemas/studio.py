from typing import Literal
from uuid import UUID

from pydantic import Field

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


class ReferenceLinkInput(InputModel):
    objective_version_id: UUID
    repository_snapshot_id: UUID
    path: str = Field(min_length=1, max_length=240)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
