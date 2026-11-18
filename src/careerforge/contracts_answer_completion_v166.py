"""Question completeness is separate from factual support and delivery completion."""

from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract

ANSWER_COMPLETION_CONTRACT = "rendered-answer-completion-1.6.6"
ANSWER_ASPECT_CONTRACT = "rendered-answer-aspects-v1"
TUTOR_PROGRESS_CONTRACT = "session-tutor-answer-progress-v1"
ANSWER_COMPLETION_STAGE = "answer.completion:" + ANSWER_COMPLETION_CONTRACT
# Server-only Turn state for evidence-only delivery, never a paid model receipt.
EVIDENCE_ANSWER_STATE = "evidence_answer_state"
EVIDENCE_CONTINUITY_CONTRACT = "evidence-answer-continuity-1.6.6"


class AnswerCompletionCheck(Contract):
    status: Literal["complete", "partial", "unknown"]
    covered_fact_ids: list[str] = Field(default_factory=list, max_length=20)
    missing_aspects: list[str] = Field(default_factory=list, max_length=10)
    reasons: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def coherent_completeness(self):
        if len(self.covered_fact_ids) != len(set(self.covered_fact_ids)):
            raise ValueError("Duplicate answer coverage anchors")
        if self.status == "complete" and (not self.covered_fact_ids or self.missing_aspects):
            raise ValueError(
                "Complete answers require accepted fact anchors and no missing aspects"
            )
        if self.status != "complete" and not self.missing_aspects:
            raise ValueError("Incomplete answers must identify a missing aspect")
        return self


class AnsweredAspect(Contract):
    requirement: str = Field(min_length=1, max_length=500)
    answer_quote: str = Field(min_length=1, max_length=1500)
    fact_atom_id: str = Field(min_length=1, max_length=180)


class AnswerAspectCheck(AnswerCompletionCheck):
    answered_aspects: list[AnsweredAspect] = Field(max_length=20)
    unsupported_aspects: list[str] = Field(max_length=10)
    conclusion_status: Literal["resolved", "partial", "safe_abstention", "unknown"]
