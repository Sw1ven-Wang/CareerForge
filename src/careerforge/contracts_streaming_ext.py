"""A provider response is one complete atomic block, never unvalidated token text."""

from pydantic import Field, model_validator

from .contracts import AnswerPart, Contract, SemanticCheck


class SingleAnswerOutput(Contract):
    blocks: list[AnswerPart] = Field(min_length=1, max_length=1)


class RelevantAnswerOutput(Contract):
    """A bounded next block selected from the remaining reviewed evidence.

    The completion flag does not assert that the repository implements anything;
    it merely stops this answer. It is never used as an authorization or review.
    """

    blocks: list[AnswerPart] = Field(max_length=1)
    complete: bool = False

    @model_validator(mode="after")
    def bounded_step(self):
        if not self.blocks and not self.complete:
            raise ValueError("An empty answer step must be complete")
        if self.blocks and self.blocks[0].kind not in {
            "repository_fact",
            "concept_fact",
            "evidence_gap",
        }:
            raise ValueError("Only reviewed facts or an evidence gap may be selected")
        return self


class RelevantSemanticCheck(SemanticCheck):
    # Relevance refers to the supplied reviewed fact, independently of whether the
    # proposed phrasing is supported. A rejected paraphrase can only fall back to
    # that fact if the fact itself addresses the current question.
    evidence_relevant: bool
