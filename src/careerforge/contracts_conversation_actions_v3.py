"""Explicit learning caller envelope; v1/v2 serialized requests remain unchanged."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract
from .contracts_conversation_actions_v1 import DialogueMessage, WorkflowControl
from .contracts_conversation_actions_v2 import ActionV2
from .contracts_session_plan_v166 import SessionPreferences


class TutorRequestInputV1(Contract):
    question: str | None = Field(default=None, min_length=1, max_length=10000)
    model_call_cap: int = Field(default=8, ge=3, le=25, strict=True)
    preferences: SessionPreferences = Field(
        default_factory=lambda: SessionPreferences(practice=False, requested_style="code_first")
    )


class LearningDialogueMessageV1(DialogueMessage):
    contract: Literal["learning-dialogue-v1"] = "learning-dialogue-v1"
    tutor_request: TutorRequestInputV1 = Field(default_factory=TutorRequestInputV1)


class LearningPlanConfirmationV1(WorkflowControl):
    contract: Literal["learning-plan-confirmation-v1"] = "learning-plan-confirmation-v1"
    tutor_request: TutorRequestInputV1 = Field(default_factory=TutorRequestInputV1)


class LearningSessionAdmissionV1(Contract):
    contract: Literal["learning-session-admission-v1"] = "learning-session-admission-v1"
    plan_id: str = Field(min_length=1, max_length=180)
    version_id: str = Field(min_length=1, max_length=180)
    module_id: str = Field(min_length=1, max_length=180)
    expected_plan_revision: int = Field(ge=1, strict=True)
    expected_profile_revision: int = Field(ge=1, strict=True)
    expected_focus_revision: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=120)
    authorization_ref: str | None = Field(default=None, min_length=1, max_length=80)
    tutor_request: TutorRequestInputV1 = Field(default_factory=TutorRequestInputV1)


class TutorRequestAuthorizationV1(WorkflowControl):
    contract: Literal["tutor-request-authorization-v1"] = "tutor-request-authorization-v1"
    request_id: str = Field(min_length=1, max_length=80)
    request_hash: str = Field(min_length=64, max_length=64)
    authorization_ref: str = Field(min_length=1, max_length=80)


class ModuleSelectionV1(WorkflowControl):
    contract: Literal["learning-module-selection-v1"] = "learning-module-selection-v1"
    plan_id: str = Field(min_length=1, max_length=180)
    version_id: str = Field(min_length=1, max_length=180)
    module_id: str = Field(min_length=1, max_length=180)
    expected_plan_revision: int = Field(ge=1, strict=True)
    expected_profile_revision: int = Field(ge=1, strict=True)
    expected_focus_revision: int = Field(ge=1, strict=True)


class LearningAction(Contract):
    type: Literal[
        "start_learning_session",
        "learning_followup",
        "authorize_tutor_request",
    ]


class SelectLearningModuleAction(Contract):
    type: Literal["select_learning_module"] = "select_learning_module"
    selector: str | None = Field(default=None, min_length=1, max_length=180)


class ActionStepV3(Contract):
    step_id: str = Field(min_length=1, max_length=40)
    action: Annotated[
        ActionV2 | LearningAction | SelectLearningModuleAction, Field(discriminator="type")
    ]
    depends_on: list[str] = Field(default_factory=list, max_length=3)


class ConversationActionPlanV3(Contract):
    contract_version: Literal["conversation-action-plan-v3"] = "conversation-action-plan-v3"
    conversation_id: str
    expected_focus_revision: int = Field(ge=1, strict=True)
    target_workflow_id: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=120)
    actions: list[ActionStepV3] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_order(self):
        seen = set()
        for step in self.actions:
            if (
                step.step_id in seen
                or not set(step.depends_on) <= seen
                or len(set(step.depends_on)) != len(step.depends_on)
            ):
                raise ValueError("Actions must have unique earlier dependencies")
            seen.add(step.step_id)
        return self
