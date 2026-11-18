"""Explicit adoption for existing conversations; legacy TurnInput stays unchanged."""

from typing import Literal

from pydantic import Field

from .contracts import Contract

ORDINARY_CHECKER_CONTRACT = "question-scoped-citation-checker-1.6.6-v1"
ORDINARY_CHECKER_KEY = "ordinary_checker_binding"


class AdoptConversationCheckerInput(Contract):
    expected_revision: int = Field(ge=1, strict=True)
    contract: Literal["question-scoped-citation-checker-1.6.6-v1"]
    idempotency_key: str = Field(min_length=1, max_length=150)
