"""Explicit, one-turn accepted-session binding. TurnInput remains unchanged."""

from pydantic import Field

from .contracts import Contract, TurnInput
from .contracts_session_plan_v166 import SessionPreferences


class SessionTutorTurnInput(TurnInput):
    expected_revision: int = Field(ge=1, strict=True)
    model_call_cap: int = Field(default=8, ge=3, le=25, strict=True)
    namespace_id: str = Field(min_length=1, max_length=180)
    plan_id: str = Field(min_length=1, max_length=180)
    version_id: str = Field(min_length=1, max_length=180)
    module_id: str = Field(min_length=1, max_length=180)
    expected_plan_revision: int = Field(ge=1, strict=True)
    preferences: SessionPreferences = Field(default_factory=SessionPreferences)


class GuidedTutorTurnInput(Contract):
    """Resolve the owned active module on the server; a learner question is optional."""

    question: str | None = Field(default=None, min_length=1, max_length=10000)
    expected_revision: int = Field(ge=1, strict=True)
    funding_id: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=150)
    model_call_cap: int = Field(default=8, ge=3, le=25, strict=True)
    preferences: SessionPreferences = Field(default_factory=SessionPreferences)
