"""Caller-only practice commands. Free text is never a submission or authorization."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract, FeedbackInput
from .contracts_conversation_actions_v1 import DialogueMessage
from .contracts_learning_ext import ActivityInput, AttemptSubmission, ObservationInput
from .contracts_session_progress_v166 import SessionModuleAckInput


class StartPractice(Contract):
    type: Literal["start_practice"] = "start_practice"
    activity: ActivityInput
    assistance_mode: Literal["assisted", "independent", "unknown"] = "assisted"
    expected_profile_revision: int = Field(ge=1, strict=True)
    module_hash: str = Field(min_length=64, max_length=64)


class PracticeTarget(Contract):
    practice_id: str
    practice_hash: str = Field(min_length=64, max_length=64)
    attempt_id: str
    specification_hash: str = Field(min_length=64, max_length=64)


class SubmitPractice(PracticeTarget):
    type: Literal["submit_practice"] = "submit_practice"
    submission: AttemptSubmission


class RequestAssessment(PracticeTarget):
    type: Literal["request_assessment"] = "request_assessment"
    submission_hash: str = Field(min_length=64, max_length=64)


class AuthorizeAssessment(Contract):
    type: Literal["authorize_assessment_request"] = "authorize_assessment_request"
    assessment_request_id: str
    request_hash: str = Field(min_length=64, max_length=64)


class PracticeObservation(PracticeTarget):
    type: Literal["record_practice_observation"] = "record_practice_observation"
    observation: ObservationInput


class PracticeFeedbackInput(FeedbackInput):
    consent: bool = False


class TutorFeedback(PracticeTarget):
    type: Literal["record_tutor_feedback"] = "record_tutor_feedback"
    feedback: PracticeFeedbackInput


class AcknowledgePractice(PracticeTarget):
    type: Literal["acknowledge_module"] = "acknowledge_module"
    acknowledgment: SessionModuleAckInput


class EndPractice(PracticeTarget):
    type: Literal["end_practice", "abandon_practice"]


PracticeIntent = Annotated[
    StartPractice
    | SubmitPractice
    | RequestAssessment
    | AuthorizeAssessment
    | PracticeObservation
    | TutorFeedback
    | AcknowledgePractice
    | EndPractice,
    Field(discriminator="type"),
]


class PracticeDialogueMessageV1(DialogueMessage):
    contract: Literal["practice-dialogue-v1"]
    workflow_id: str
    execution_id: str
    expected_workflow_revision: int = Field(ge=1, strict=True)
    intent: PracticeIntent

    @model_validator(mode="after")
    def funding_is_explicit_assessment_only(self):
        if self.authorization_ref and self.intent.type not in {
            "request_assessment",
            "authorize_assessment_request",
        }:
            raise ValueError("Only explicit assessment commands accept funding")
        return self
