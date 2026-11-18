"""Opt-in research sufficiency contracts, separate from factual support.

The old AnalysisOutput, ReviewOutput and CoverageOutput are intentionally not
changed. A frozen research-task-1.6.3 manifest selects these schemas; historical
model receipts continue to use their original request schema and prompt.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, model_validator

from .contracts import AnalysisOutput, Contract, ExtractedRequirement, ReviewOutput, UnitDraft
from .contracts_jd_v166 import ProvenanceRequirement

RESEARCH_CONTRACT_VERSION: Final = "research-task-1.6.3"
RESEARCH_COVERAGE_VERSION: Final = "research-coverage-1.6.3"

ResearchScope = Literal["project", "focused", "concept"]
ResearchDimension = Literal[
    "purpose",
    "project_structure",
    "module_responsibilities",
    "call_flow",
    "implementation",
    "boundaries",
    "tradeoffs",
]
ResearchDepth = Literal["了解", "熟悉", "掌握"]
CoverageStatus = Literal["known", "partial", "unknown", "not_applicable", "conflicting"]


class ResearchRequirement(ExtractedRequirement):
    # Stable within the frozen goal version. A topic is not a requirement ID.
    requirement_id: str = Field(pattern=r"^[a-z0-9_.-]{1,100}$")
    required_dimensions: list[ResearchDimension] = Field(min_length=1, max_length=7)


class ProvenanceResearchRequirement(ProvenanceRequirement):
    # Old ResearchRequirement serialization stays byte-identical. Only imported
    # JD revisions select this richer type; absent depth is never filled in.
    required_dimensions: list[ResearchDimension] = Field(default_factory=list, max_length=7)


class ResearchTaskContract(Contract):
    contract_version: Literal["research-task-1.6.3"] = RESEARCH_CONTRACT_VERSION
    original_question: str = Field(min_length=1, max_length=6000)
    scope: ResearchScope = "project"
    goal_version_id: str | None = Field(default=None, min_length=1, max_length=180)
    requirements: list[ResearchRequirement | ProvenanceResearchRequirement] = Field(
        default_factory=list, max_length=30
    )
    unresolved_requirement_ids: list[str] = Field(default_factory=list, max_length=60)
    required_dimensions: list[ResearchDimension] = Field(default_factory=list, max_length=7)

    @model_validator(mode="after")
    def unique_requirements_and_dimensions(self):
        ids = [requirement.requirement_id for requirement in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate research requirement IDs")
        if len(self.required_dimensions) != len(set(self.required_dimensions)):
            raise ValueError("Duplicate research dimensions")
        return self


class ResearchClaimAnchor(Contract):
    """A candidate-local reference, resolved to published claims by the server."""

    subject_key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,160}$")
    claim_keys: list[str] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def unique_claim_keys(self):
        if len(self.claim_keys) != len(set(self.claim_keys)):
            raise ValueError("Duplicate anchored claim keys")
        return self


class ResearchEvidenceAssessment(Contract):
    status: CoverageStatus
    explanation: str = Field(min_length=1, max_length=2000)
    anchors: list[ResearchClaimAnchor] = Field(default_factory=list, max_length=5)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=10)


class ResearchDimensionAssessment(ResearchEvidenceAssessment):
    dimension: ResearchDimension


class ResearchRequirementAssessment(ResearchEvidenceAssessment):
    requirement_id: str = Field(pattern=r"^[a-z0-9_.-]{1,100}$")
    demonstrated_depth: ResearchDepth | None = None
    dimensions: list[ResearchDimensionAssessment] = Field(default_factory=list, max_length=7)

    @model_validator(mode="after")
    def unique_dimensions(self):
        values = [dimension.dimension for dimension in self.dimensions]
        if len(values) != len(set(values)):
            raise ValueError("Duplicate requirement dimensions")
        return self


class ResearchCoverageDraft(Contract):
    contract_version: Literal["research-coverage-1.6.3"] = RESEARCH_COVERAGE_VERSION
    question: ResearchEvidenceAssessment
    dimensions: list[ResearchDimensionAssessment] = Field(default_factory=list, max_length=7)
    requirements: list[ResearchRequirementAssessment] = Field(default_factory=list, max_length=30)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def unique_assessments(self):
        for keys in (
            [entry.dimension for entry in self.dimensions],
            [entry.requirement_id for entry in self.requirements],
        ):
            if len(keys) != len(set(keys)):
                raise ValueError("Duplicate research coverage assessments")
        return self

    def assessments(self) -> list[ResearchEvidenceAssessment]:
        return [
            self.question,
            *self.dimensions,
            *self.requirements,
            *(dimension for req in self.requirements for dimension in req.dimensions),
        ]


class ResearchAnalysisOutput(AnalysisOutput):
    # No usable evidence is a legitimate partial outcome, never a reason to
    # manufacture the minimum one unit required by the historical schema.
    units: list[UnitDraft] = Field(default_factory=list, max_length=5)
    task_coverage: ResearchCoverageDraft = Field(
        default_factory=lambda: ResearchCoverageDraft(
            question=ResearchEvidenceAssessment(status="unknown", explanation="not_reported")
        )
    )

    @model_validator(mode="after")
    def candidate_local_anchors(self):
        self.task_coverage.unresolved_questions = list(
            dict.fromkeys(
                [
                    *self.task_coverage.unresolved_questions,
                    *self.unanswered_questions,
                ]
            )
        )
        if len(self.task_coverage.unresolved_questions) > 30:
            raise ValueError("Too many unresolved research questions")
        keys = {unit.subject_key: {claim.key for claim in unit.claims} for unit in self.units}
        if len(keys) != len(self.units):
            raise ValueError("Research candidates need unique subject keys within one source")
        for assessment in self.task_coverage.assessments():
            for anchor in assessment.anchors:
                if not set(anchor.claim_keys) <= keys.get(anchor.subject_key, set()):
                    raise ValueError("Research coverage references an unknown candidate claim")
        return self


class ResearchReviewOutput(ReviewOutput):
    # This is an independent sufficiency assessment of the original task, not a
    # replacement for claim decisions or the existing fact-binding audit.
    task_coverage: ResearchCoverageDraft = Field(
        default_factory=lambda: ResearchCoverageDraft(
            question=ResearchEvidenceAssessment(status="unknown", explanation="not_reported")
        )
    )


class ResearchSourceCoverage(Contract):
    source_id: str = Field(min_length=1, max_length=180)
    coverage: ResearchCoverageDraft


class ResearchSufficiencyOutput(Contract):
    """Final composition audit over already published, independently read facts.

    The server supplies the allowed source/unit identities. No model output can
    add publications, change factual support, or broaden the task's authority.
    """

    source_coverages: list[ResearchSourceCoverage] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def unique_sources(self):
        source_ids = [item.source_id for item in self.source_coverages]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("Duplicate source sufficiency assessments")
        return self
