"""Opt-in enriched actions. V1 model dumps and request identities stay unchanged."""

from typing import Annotated, Literal

from pydantic import Field

from .contracts import Contract
from .contracts_conversation_actions_v1 import (
    ActionPlanBase,
    ClarifyAction,
    ImportJDAction,
    ScopedAction,
    SwitchAction,
    TargetAction,
    WorkflowControl,
)
from .contracts_goal_amendment_v1 import Depth, Priority, SourceSpan


class GoalObjectiveAction(Contract):
    type: Literal["add_learning_objective"] = "add_learning_objective"
    target: str = Field(min_length=1, max_length=120)
    target_depth: Depth | None = None
    learning_priority: Priority = "unspecified"
    target_span: SourceSpan | None = None
    depth_span: SourceSpan | None = None
    priority_span: SourceSpan | None = None


class CancelWorkflowAction(Contract):
    type: Literal["cancel_workflow"] = "cancel_workflow"


ActionV2 = Annotated[
    ScopedAction
    | ImportJDAction
    | TargetAction
    | SwitchAction
    | ClarifyAction
    | GoalObjectiveAction
    | CancelWorkflowAction,
    Field(discriminator="type"),
]


class ActionStepV2(Contract):
    step_id: str = Field(min_length=1, max_length=40)
    action: ActionV2
    depends_on: list[str] = Field(default_factory=list, max_length=3)


class ConversationActionPlanV2(ActionPlanBase[ActionStepV2]):
    contract_version: Literal["conversation-action-plan-v2"] = "conversation-action-plan-v2"


class GoalAmendmentInput(WorkflowControl):
    source_goal_version_id: str = Field(min_length=1, max_length=80)
    expected_goal_revision: int = Field(ge=1, strict=True)
    topic: str = Field(min_length=1, max_length=120)
    target_depth: Depth | None = None
    learning_priority: Priority = "unspecified"
