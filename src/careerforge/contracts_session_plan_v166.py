"""Opt-in single-session budgets without changing historical planner schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, model_validator

from .contracts import Contract
from .contracts_learning_ext import LearningIntent


class SessionPreferences(Contract):
    practice: StrictBool | None = None
    check_understanding: StrictBool | None = None
    diagnostics_enabled: StrictBool | None = None
    requested_style: (
        Literal[
            "concept_first",
            "code_first",
            "comparison_first",
            "architecture_first",
            "evidence_first",
        ]
        | None
    ) = None


class SessionPlanInput(Contract):
    namespace_id: str
    funding_id: str | None = None
    publication_ids: list[str] = Field(default_factory=list, max_length=20)
    learning_intent: LearningIntent
    title: str = Field(min_length=1, max_length=200)
    total_minutes: int = Field(ge=1, le=240)
    preferences: SessionPreferences = Field(default_factory=SessionPreferences)
    expected_profile_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=150)

    @model_validator(mode="after")
    def paid_generation_requires_funding(self):
        if self.publication_ids and self.total_minutes >= 5 and not self.funding_id:
            raise ValueError("A buildable session needs explicit funding")
        return self
