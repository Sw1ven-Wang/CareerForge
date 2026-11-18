"""No authority-bearing parameters in coordinator actions."""

from typing import Literal

from pydantic import Field

from .contracts import Contract


class PracticeAction(Contract):
    type: Literal[
        "start_practice",
        "submit_practice",
        "request_assessment",
        "authorize_assessment_request",
        "record_practice_observation",
        "record_tutor_feedback",
        "acknowledge_module",
        "end_practice",
        "abandon_practice",
    ]


class PracticeStep(Contract):
    step_id: str
    action: PracticeAction
    depends_on: list[str] = []


class ConversationActionPlanV4(Contract):
    contract_version: Literal["conversation-action-plan-v4"] = "conversation-action-plan-v4"
    conversation_id: str
    expected_focus_revision: int = Field(ge=1, strict=True)
    target_workflow_id: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=120)
    actions: list[PracticeStep] = Field(min_length=1, max_length=1)
