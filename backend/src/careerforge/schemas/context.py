"""Contracts for context assembly, evidence recall and a single bounded role task."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from careerforge.domain.json_types import JsonObject as Json
from careerforge.schemas.resources import InputModel


class ContextReadArgs(InputModel):
    evidence_id: str = Field(min_length=1, max_length=100)
    offset: int = Field(default=0, ge=0, le=2000000)
    limit: int = Field(default=2500, ge=100, le=3500)


class SkillReadArgs(InputModel):
    skill_id: Literal["trace_implementation", "failure_boundary", "practice_evidence"]


class EvidencePointer(InputModel):
    evidence_id: str
    kind: Literal["tutor_turn", "attempt", "skill"]
    source_id: str
    snapshot_id: UUID | None = None
    version: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: str = Field(max_length=500)
    verification: str


class ContextTaskContract(InputModel):
    task_id: UUID
    parent_run_id: UUID | None = None
    role: Literal["tutor"] = "tutor"
    requirements_id: UUID
    plan_id: UUID
    module_key: str
    module_hash: str
    learning_intent: Json = Field(default_factory=dict)
    reading_selection: Json = Field(default_factory=dict)
    scope: list[str]
    allowed_tools: list[str]
    help_mode: Literal["assistance_allowed"] = "assistance_allowed"
    model_call_limit: int = Field(ge=1, le=3)
    max_output_tokens: int
    tool_limit: Literal[7] = 7
    code_read_limit: Literal[4] = 4
    recall_read_limit: Literal[3] = 3
    stop_conditions: list[str]


class ContextManifest(InputModel):
    version: Literal["context-1.0"] = "context-1.0"
    task: ContextTaskContract
    areas: Json
    evidence: list[EvidencePointer]
    omitted: list[str]
    measurement: Json
    payload_hash: str
    competence_verified: Literal[False] = False


class ContextEvent(InputModel):
    action: Literal["assembled", "evidence_read", "skill_loaded"]
    task_id: str
    reference: str
    content_hash: str
    characters: int = Field(ge=0)
