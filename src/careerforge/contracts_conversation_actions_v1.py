"""Bounded action proposals. Caller authorization lives outside the plan."""

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import Field, model_serializer, model_validator

from .contracts import Contract
from .contracts_learning_ext import AttemptSubmission, ObservationInput
from .contracts_session_progress_v166 import SessionModuleAckInput


class DialogueMessage(Contract):
    text: str = Field(min_length=1, max_length=50_000)
    expected_focus_revision: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=120)
    # These are explicit caller inputs. No coordinator/model plan carries these fields.
    authorization_ref: str | None = Field(default=None, min_length=1, max_length=80)
    source_id: str | None = Field(default=None, min_length=1, max_length=80)
    source_ids: list[str] = Field(default_factory=list, max_length=2)
    build_call_cap: int = Field(default=24, ge=8, le=100, strict=True)
    # Admission-only choices, never authority supplied by a coordinator action.
    research_question: str | None = Field(default=None, min_length=1, max_length=6000)
    build_wall_time_contract: Literal["unlimited-build-time-v1"] | None = None
    auto_evidence_retry: bool = Field(default=False, strict=True)
    kind: Literal["auto", "jd", "repository", "topic"] = "auto"
    observation: ObservationInput | None = None
    submission: AttemptSubmission | None = None
    module_ack: SessionModuleAckInput | None = None
    publication_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_serializer(mode="wrap")
    def serialize_build_time_choice(self, handler):
        # Absent/default opt-in preserves pre-contract JSON and idempotency hashes.
        data = handler(self)
        if self.build_wall_time_contract is None:
            data.pop("build_wall_time_contract", None)
        return data

    @model_validator(mode="after")
    def validate_sources(self):
        if (self.source_id and self.source_ids) or len(set(self.source_ids)) != len(
            self.source_ids
        ):
            raise ValueError("Use unique frozen sources in either source_id or source_ids")
        if any(not 1 <= len(item) <= 80 for item in self.source_ids):
            raise ValueError("Invalid source identity")
        if self.research_question is not None and (
            self.source_id
            or self.source_ids
            or self.kind == "repository"
            or self.auto_evidence_retry
        ):
            raise ValueError(
                "Research question requires search admission, not a fixed source or retry"
            )
        if self.auto_evidence_retry and len(self.source_ids) > 1:
            raise ValueError("Evidence retry requires exactly one frozen source")
        return self


class WorkflowControl(Contract):
    expected_revision: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=120)
    authorization_ref: str | None = Field(default=None, min_length=1, max_length=80)


class WorkflowFocus(Contract):
    workflow_id: str = Field(min_length=1, max_length=80)
    expected_focus_revision: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=120)


class GoalRevisionInput(WorkflowControl):
    source_goal_version_id: str | None = Field(default=None, max_length=80)
    topic: str = Field(min_length=1, max_length=120)


class ScopedAction(Contract):
    type: Literal[
        "use_reviewed_material",
        "submit_practice_answer",
        "assess_practice",
        "advance_learning_module",
        "select_implementation_language",
        "start_project_discovery",
        "query_progress",
        "start_extraction",
        "request_pause",
        "resume_workflow",
        "start_build",
        "retry_build",
        "propose_plan",
        "accept_plan",
        "start_tutor",
        "start_practice",
        "record_feedback",
        "propose_goal_revision",
    ]


class ImportJDAction(Contract):
    type: Literal["import_jd"] = "import_jd"
    text: str = Field(min_length=1, max_length=50_000)


class TargetAction(Contract):
    type: Literal["add_learning_topic", "set_learning_topic", "set_repository"]
    target: str = Field(min_length=1, max_length=300)


class SwitchAction(Contract):
    type: Literal["switch_focus"] = "switch_focus"
    mode: Literal["new", "previous", "selected"] = "new"


class ClarifyAction(Contract):
    type: Literal["ask_clarification"] = "ask_clarification"
    slot: Literal["target_to_modify", "new_jd_or_topic", "scope_or_confirmation"]


Action = Annotated[
    ScopedAction | ImportJDAction | TargetAction | SwitchAction | ClarifyAction,
    Field(discriminator="type"),
]


class ActionStep(Contract):
    step_id: str = Field(min_length=1, max_length=40)
    action: Action
    depends_on: list[str] = Field(default_factory=list, max_length=3)


Step = TypeVar("Step")


class ActionPlanBase(Contract, Generic[Step]):
    contract_version: str
    conversation_id: str
    expected_focus_revision: int = Field(ge=1, strict=True)
    target_workflow_id: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=120)
    actions: list[Step] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_order(self):
        seen = set()
        for step in self.actions:
            if step.step_id in seen or not set(step.depends_on) <= seen:
                raise ValueError("Actions must be unique and depend only on earlier steps")
            if len(set(step.depends_on)) != len(step.depends_on):
                raise ValueError("Duplicate action dependency")
            seen.add(step.step_id)
        return self


class ConversationActionPlan(ActionPlanBase[ActionStep]):
    contract_version: Literal["conversation-action-plan-v1"] = "conversation-action-plan-v1"
