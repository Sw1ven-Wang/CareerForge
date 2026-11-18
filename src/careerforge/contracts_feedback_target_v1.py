"""Explicit new-conversation opt-in; legacy conversation/feedback inputs stay closed."""

from typing import Literal

from pydantic import Field, StrictBool

from .contracts import Contract

FEEDBACK_TARGET_CONTRACT = "answer-feedback-continuation-1.6.6-v1"
FEEDBACK_TARGET_CHOICE = "answer_feedback_contract"
FEEDBACK_TARGET_KEY = "answer_feedback_binding"


class AnswerFeedbackConversationInput(Contract):
    contract: Literal["answer-feedback-continuation-1.6.6-v1"]
    auto_build: StrictBool = False
    build_call_cap: int = Field(default=24, ge=8, le=100, strict=True)


class AnswerContinuationFeedbackInput(Contract):
    contract: Literal["answer-feedback-continuation-1.6.6-v1"]
    turn_id: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=3000)
    consent: StrictBool
    idempotency_key: str = Field(min_length=1, max_length=150)
