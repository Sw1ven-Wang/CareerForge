"""Versioned deterministic C05 presentation, never a model-output schema."""

from typing import Final, Literal

from pydantic import Field

from .contracts import Contract
from .contracts_research_v163 import ResearchAnalysisOutput

PROJECT_ANALYSIS_VERSION: Final = "reviewed-project-analysis-1.6.6"
PROJECT_ANALYSIS_FIELDS: Final = (
    "project_analysis_contract_version",
    "projection",
    "repo_map",
    "modules",
    "call_chain_edges",
    "exception_boundaries",
    "key_symbols",
    "knowledge_units",
    "jd_mechanism_evidence_links",
    "open_questions",
)


class MechanismEdgeDraft(Contract):
    subject_key: str = Field(min_length=1, max_length=120)
    claim_key: str = Field(min_length=1, max_length=120)
    from_path: str = Field(min_length=1, max_length=1000)
    from_symbol: str = Field(min_length=1, max_length=200)
    to_path: str = Field(min_length=1, max_length=1000)
    to_symbol: str = Field(min_length=1, max_length=200)


class MechanismAnalysisOutput(ResearchAnalysisOutput):
    mechanism_edges: list[MechanismEdgeDraft] = Field(max_length=3)


class ReviewedStructureSection(Contract):
    """Items are exact reviewed claims, not newly inferred module/edge objects.

    Each item's source/unit/claim/citation binding is validated by the service.
    The compatibility name call_chain_edges does not imply typed graph edges.
    """

    dimension: str
    status: Literal["known", "partial", "unknown"]
    representation: Literal["scope_bound_reviewed_claim_narratives"] = (
        "scope_bound_reviewed_claim_narratives"
    )
    items: list[dict] = Field(default_factory=list)
    scoped_assessments: list[dict] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    typed_edges_available: Literal[False] = False
    full_repository_coverage: Literal[False] = False


class ProjectAnalysisProjection(Contract):
    project_analysis_contract_version: Literal["reviewed-project-analysis-1.6.6"] = (
        PROJECT_ANALYSIS_VERSION
    )
    projection: dict
    repo_map: dict
    modules: ReviewedStructureSection
    call_chain_edges: ReviewedStructureSection
    exception_boundaries: ReviewedStructureSection
    key_symbols: dict
    knowledge_units: list[dict]
    jd_mechanism_evidence_links: dict
    open_questions: list[str]
