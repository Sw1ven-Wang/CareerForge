"""Scoped candidate review and teaching probes; these never authorize publication."""

from typing import Literal

from pydantic import Field

from .contracts import Contract, ReadReference


class CuratedClaimDecision(Contract):
    unit_version_id: str
    claim_key: str
    outcome: Literal[
        "supported_within_scope", "needs_revision", "insufficient_evidence", "contradicted"
    ]
    explanation: str = Field(min_length=4)
    independent_reads: list[ReadReference] = Field(max_length=6)
    required_correction: str | None


class CuratedReviewOutput(Contract):
    decisions: list[CuratedClaimDecision] = Field(min_length=1, max_length=16)
    mechanism_boundary: str = Field(min_length=4)
    unresolved_questions: list[str] = Field(max_length=10)


class TrialCitation(Contract):
    unit_version_id: str
    read_id: str
    quote: str = Field(min_length=4, max_length=2600)


class ReadingCheck(Contract):
    question: str = Field(min_length=4)
    expected_points: list[str] = Field(min_length=1, max_length=6)


class CuratedTutorOutput(Contract):
    answer: str = Field(min_length=10)
    citations: list[TrialCitation] = Field(min_length=1, max_length=10)
    reading_check: ReadingCheck
    limitations: list[str] = Field(min_length=1, max_length=8)
    learner_mastery_demonstrated: Literal[False]


class CuratedTrialCheck(Contract):
    correctness: Literal["pass", "fail"]
    code_navigation: Literal["pass", "fail"]
    boundary_handling: Literal["pass", "fail"]
    reading_check_quality: Literal["pass", "fail"]
    issues: list[str] = Field(max_length=12)
    rationale: str = Field(min_length=10)
    evidence: list[ReadReference] = Field(min_length=1, max_length=8)
