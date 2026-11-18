"""Opt-in question observables, separate from every historical model schema.

Request excerpts identify a question and its observables. They are not new
instructions, expected answers, executable scenarios, or reviewed source facts.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract
from .contracts_learning_ext import (
    PlanGoalGapDraft,
    PlanScenarioDraft,
    PlanTextReference,
    RubricKey,
)
from .contracts_learning_plan_v162 import PlanTaskAnchorsV162

QUESTION_SCOPE = "question_observables"
QUESTION_PLAN_CONTRACT = "learning-planner-question-observables-1.6.6"
QUESTION_DRAFT_VERSION = "learning-plan-question-observables-1.6.6"
QUESTION_TASK_SCHEMA = "learning-task-question-observables-1.6.6"
QUESTION_ACTIVITY_SCHEMA = "learning-activity-question-observables-1.6.6"


class QuestionObservableReference(PlanTextReference):
    quote: str = Field(min_length=1, max_length=120)


class QuestionCheckAnchors(Contract):
    observable_identity: QuestionObservableReference
    targets: list[PlanTextReference] = Field(min_length=1, max_length=4)


class QuestionConditionCheck(QuestionCheckAnchors):
    rubric_key: Literal["conditions"]
    observable: Literal["state_condition"]


class QuestionResultCheck(QuestionCheckAnchors):
    rubric_key: Literal["execution_path"]
    observable: Literal["state_result"]


class QuestionMembersCheck(QuestionCheckAnchors):
    rubric_key: Literal["conditions"]
    observable: Literal["identify_members"]


class QuestionRuntimeCheck(QuestionCheckAnchors):
    rubric_key: Literal["runtime"]
    observable: Literal["verify_runtime_receipt"]
    targets: list[PlanTextReference] = Field(default_factory=list, max_length=0)


QuestionStaticCheck = QuestionConditionCheck | QuestionResultCheck | QuestionMembersCheck


class QuestionTaskAnchors(PlanTaskAnchorsV162):
    question: PlanTextReference
    # Original question data already contains the scenario. No new scenario,
    # changed input, or variant may silently replace it in this narrow contract.
    scenarios: list[PlanScenarioDraft] = Field(default_factory=list, max_length=0)


class QuestionDiagnosticTask(QuestionTaskAnchors):
    kind: Literal["diagnostic"]
    acceptance_checks: list[QuestionStaticCheck] = Field(min_length=1, max_length=6)


class QuestionUnderstandingTask(QuestionTaskAnchors):
    kind: Literal["understanding_check"]
    acceptance_checks: list[QuestionStaticCheck] = Field(min_length=1, max_length=6)


class QuestionExerciseTask(QuestionTaskAnchors):
    kind: Literal["exercise"]
    acceptance_checks: list[QuestionStaticCheck | QuestionRuntimeCheck] = Field(
        min_length=1, max_length=6
    )


class QuestionModuleDraft(Contract):
    key: str = Field(min_length=1, max_length=40, pattern=r"^[a-zA-Z0-9_-]+$")
    publication_id: str
    focus_fact_ids: list[str] = Field(min_length=1, max_length=12)
    prerequisite_keys: list[str] = Field(default_factory=list, max_length=20)
    rubric_keys: list[RubricKey] = Field(min_length=1, max_length=6)
    estimated_minutes: int = Field(ge=5, le=240)
    # Broad trace/transfer steps would reintroduce the expanded task in display.
    steps: list[Literal["read_reviewed"]] = Field(min_length=1, max_length=1)
    task: QuestionDiagnosticTask | QuestionUnderstandingTask | QuestionExerciseTask
    rationale_codes: list[Literal["focus_on_function", "self_reported_context"]] = Field(
        default_factory=list, max_length=2
    )

    @model_validator(mode="after")
    def no_duplicates(self) -> QuestionModuleDraft:
        for values in (self.focus_fact_ids, self.prerequisite_keys, self.steps, self.rubric_keys):
            if len(set(values)) != len(values):
                raise ValueError("Duplicate question module entries")
        return self


class QuestionPlanDraft(Contract):
    modules: list[QuestionModuleDraft] = Field(min_length=1, max_length=20)
    uncovered_goal_requirements: list[
        PlanGoalGapDraft | Annotated[str, Field(min_length=1, max_length=2000)]
    ] = Field(default_factory=list, max_length=20)
