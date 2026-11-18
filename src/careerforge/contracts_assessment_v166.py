"""Model assessment is a disputable candidate, never human review or mastery."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .contracts import Contract


class AssessmentInput(Contract):
    namespace_id: str
    funding_id: str
    attempt_id: str
    idempotency_key: str = Field(min_length=1, max_length=150)


class AssessmentItemDraft(Contract):
    rubric_item_id: str
    outcome: Literal["demonstrated", "not_yet", "unknown"]
    submission_quote: str = Field(default="", max_length=2000)
    fact_atom_ids: list[str] = Field(default_factory=list, max_length=12)
    explanation: str = Field(min_length=1, max_length=1500)


class AssessmentDraft(Contract):
    rubric_items: list[AssessmentItemDraft] = Field(min_length=1, max_length=6)
    limitations: list[str] = Field(default_factory=list, max_length=10)
