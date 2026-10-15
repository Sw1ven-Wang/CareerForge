"""Requirement → reviewed project → modular learning contracts."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from careerforge.schemas.resources import InputModel

Depth = Literal["了解", "熟悉", "掌握"]
Intent = Literal["build_project", "learn_project", "technical_design", "function"]
Role = Literal["extract", "discover", "review", "plan", "tutor"]


class TrackInput(InputModel):
    name: str = Field(min_length=1, max_length=100)
    source_snapshot_ids: list[UUID] = Field(min_length=1, max_length=5)
    target_count: int = Field(default=5, ge=1, le=10)


class TrackSettings(InputModel):
    expected_revision: int = Field(ge=1)
    target_count: int = Field(ge=1, le=10)


class Evidence(InputModel):
    source_snapshot_id: str
    quote: str = Field(min_length=4, max_length=1000)


class TechnicalRequirement(InputModel):
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    technology: str = Field(min_length=1, max_length=100)
    keywords: list[str] = Field(min_length=1, max_length=8)
    depth: Depth
    depth_basis: Literal["explicit", "inferred"]
    rationale: str = Field(min_length=1, max_length=600)
    evidence: list[Evidence] = Field(min_length=1, max_length=5)


class ExtractOutput(InputModel):
    requirements: list[TechnicalRequirement] = Field(min_length=1, max_length=25)
    limitations: list[str] = Field(default_factory=list, max_length=10)


class SearchQuery(InputModel):
    query: str = Field(min_length=2, max_length=180)
    requirement_keys: list[str] = Field(min_length=1, max_length=8)
    purpose: str = Field(min_length=1, max_length=300)


class DiscoveryOutput(InputModel):
    queries: list[SearchQuery] = Field(min_length=1, max_length=3)


class CodeEvidence(InputModel):
    requirement_key: str
    path: str = Field(min_length=1, max_length=1000)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: str = Field(min_length=8, max_length=1000)
    explanation: str = Field(min_length=1, max_length=1000)


class ReviewOutput(InputModel):
    verdict: Literal["strong", "weak", "uncertain"]
    summary: str = Field(min_length=1, max_length=1000)
    technology_stack: list[str] = Field(min_length=1, max_length=20)
    quality_notes: list[str] = Field(min_length=1, max_length=8)
    matches: list[CodeEvidence] = Field(default_factory=list, max_length=6)
    limitations: list[str] = Field(default_factory=list, max_length=10)


class LearningIntent(InputModel):
    focus_chunk_id: UUID | None = None
    kind: Intent
    requirement_keys: list[str] = Field(min_length=1, max_length=25)
    project_review_ids: list[UUID] = Field(min_length=1, max_length=10)
    focus: str = Field(default="", max_length=1500)

    @model_validator(mode="after")
    def needs_focus(self):
        if self.kind in {"technical_design", "function"} and not self.focus.strip():
            raise ValueError("请填写具体设计或函数目标")
        if self.kind == "function" and not self.focus_chunk_id:
            raise ValueError("请从项目索引选择具体函数或实现片段")
        return self


class Module(InputModel):
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    parent_key: str | None = None
    title: str = Field(min_length=1, max_length=120)
    depth: Depth
    requirement_keys: list[str] = Field(min_length=1, max_length=8)
    project_review_ids: list[str] = Field(min_length=1, max_length=5)
    highlight_ids: list[str] = Field(default_factory=list, max_length=8)
    prerequisite_keys: list[str] = Field(default_factory=list, max_length=8)
    steps: list[str] = Field(min_length=1, max_length=8)
    deliverable: str = Field(min_length=1, max_length=1000)
    acceptance: list[str] = Field(min_length=1, max_length=6)


class PlanOutput(InputModel):
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=1500)
    modules: list[Module] = Field(min_length=1, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=10)


class GraphNode(InputModel):
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,30}$")
    label: str = Field(min_length=1, max_length=100)


class GraphEdge(InputModel):
    source: str
    target: str
    label: str = Field(default="", max_length=80)


class VisualBlock(InputModel):
    kind: Literal["text", "diagram", "table"]
    title: str = Field(default="", max_length=120)
    text: str = Field(default="", max_length=6000)
    nodes: list[GraphNode] = Field(default_factory=list, max_length=12)
    edges: list[GraphEdge] = Field(default_factory=list, max_length=24)
    columns: list[str] = Field(default_factory=list, max_length=6)
    rows: list[list[str]] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def valid_block(self):
        keys = [n.key for n in self.nodes]
        if self.kind == "diagram" and (
            not keys
            or len(keys) != len(set(keys))
            or any(e.source not in keys or e.target not in keys for e in self.edges)
        ):
            raise ValueError("invalid diagram")
        if self.kind == "table" and (
            not self.columns or any(len(r) != len(self.columns) for r in self.rows)
        ):
            raise ValueError("invalid table")
        if self.kind == "text" and not self.text.strip():
            raise ValueError("empty text")
        return self


class TutorPoint(InputModel):
    title: str = Field(min_length=1, max_length=120)
    citation_id: str
    quote: str = Field(min_length=8, max_length=1000)
    explanation: str = Field(min_length=1, max_length=2000)


class TutorOutput(InputModel):
    status: Literal["grounded", "insufficient_evidence"]
    blocks: list[VisualBlock] = Field(min_length=1, max_length=6)
    points: list[TutorPoint] = Field(default_factory=list, max_length=5)
    focus_citation_id: str | None = None
    questions: list[str] = Field(default_factory=list, max_length=6)
    limitations: list[str] = Field(default_factory=list, max_length=10)


class RoleInput(InputModel):
    auto_review_count: int = Field(default=0, ge=0, le=5)
    expected_revision: int = Field(ge=1)
    role: Role
    candidate_id: UUID | None = None
    intent: LearningIntent | None = None
    plan_id: UUID | None = None
    module_key: str | None = Field(default=None, max_length=40)
    question: str = Field(default="", max_length=3000)
    max_output_tokens: int = Field(default=4096, ge=512, le=4096)
    model_call_limit: int = Field(default=3, ge=1, le=3)


class CandidateInput(InputModel):
    expected_revision: int = Field(ge=1)
    repository_snapshot_id: UUID


class ModuleProgress(InputModel):
    expected_revision: int = Field(ge=1)
    plan_id: UUID
    module_key: str = Field(max_length=40)
    state: Literal["pending", "learning", "completed"]
    note: str = Field(default="", max_length=1000)
