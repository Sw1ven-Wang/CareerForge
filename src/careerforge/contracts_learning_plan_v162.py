"""Explicit planner contract for new 1.6.2 requests only.

Keep the 1.6.1 classes untouched: their JSON Schema is part of the gateway's
receipt hash. Literal tags make the new anyOf branches mutually exclusive while
remaining compatible with the provider's strict-schema subset. The same branches
validate responses, so a reading label cannot hide an assessed task.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract
from .contracts_learning_ext import (
    PlanAcceptanceDraft,
    PlanGoalGapDraft,
    PlanScenarioDraft,
    PlanTextReference,
    PlanTitleReference,
    RubricKey,
    StepKind,
)

PLAN_CONTRACT_VERSION = "learning-planner-1.6.2"
PLAN_DRAFT_VERSION = "learning-plan-draft-1.6.2"


class PlanVersionCheckV162(PlanAcceptanceDraft):
    rubric_key: Literal["evidence_and_version"]
    observable: Literal["cite_version"]


class PlanConditionsCheckV162(PlanAcceptanceDraft):
    rubric_key: Literal["conditions"]
    observable: Literal["explain_conditions", "contrast_boundary"]


class PlanTraceCheckV162(PlanAcceptanceDraft):
    rubric_key: Literal["execution_path"]
    observable: Literal["trace_input_output"]


class PlanCounterexampleCheckV162(PlanAcceptanceDraft):
    rubric_key: Literal["counterexample"]
    observable: Literal["identify_counterexample"]


class PlanTransferCheckV162(PlanAcceptanceDraft):
    rubric_key: Literal["transfer"]
    observable: Literal["explain_variant"]


class PlanRuntimeCheckV162(PlanAcceptanceDraft):
    rubric_key: Literal["runtime"]
    observable: Literal["verify_runtime_receipt"]


PlanNonExerciseCheckV162 = (
    PlanVersionCheckV162
    | PlanConditionsCheckV162
    | PlanTraceCheckV162
    | PlanCounterexampleCheckV162
)
PlanExerciseCheckV162 = PlanNonExerciseCheckV162 | PlanTransferCheckV162 | PlanRuntimeCheckV162


class PlanTaskAnchorsV162(Contract):
    # Separate versioned models avoid changing legacy schema bytes and avoid
    # treating a narrower mutable list as a subtype of the old flat task model.
    kind: Literal["reading", "diagnostic", "understanding_check", "exercise", "transfer"]
    title: PlanTitleReference | None = None
    inputs: list[PlanTextReference] = Field(default_factory=list, max_length=8)
    scenarios: list[PlanScenarioDraft] = Field(default_factory=list, max_length=6)
    boundaries: list[PlanTextReference] = Field(default_factory=list, max_length=8)


class PlanReadingTaskV162(PlanTaskAnchorsV162):
    """Reading presents excerpts only, with no scenarios or acceptance checks."""

    kind: Literal["reading"]
    scenarios: list[PlanScenarioDraft] = Field(default_factory=list, max_length=0)
    acceptance_checks: list[PlanNonExerciseCheckV162] = Field(default_factory=list, max_length=0)


class PlanDiagnosticTaskV162(PlanTaskAnchorsV162):
    """Requires diagnostics_enabled; transfer/runtime checks are not diagnostic."""

    kind: Literal["diagnostic"]
    acceptance_checks: list[PlanNonExerciseCheckV162] = Field(default_factory=list, max_length=6)


class PlanUnderstandingTaskV162(PlanTaskAnchorsV162):
    """Requires checks_enabled; transfer/runtime checks are not understanding checks."""

    kind: Literal["understanding_check"]
    acceptance_checks: list[PlanNonExerciseCheckV162] = Field(default_factory=list, max_length=6)


class PlanExerciseTaskV162(PlanTaskAnchorsV162):
    """Requires exercises_enabled; runtime work still needs separate approval."""

    kind: Literal["exercise"]
    acceptance_checks: list[PlanExerciseCheckV162] = Field(default_factory=list, max_length=6)


class PlanTransferTaskV162(PlanTaskAnchorsV162):
    """Requires exercises_enabled; a transfer task cannot be relabeled as reading."""

    kind: Literal["transfer"]
    acceptance_checks: list[PlanExerciseCheckV162] = Field(default_factory=list, max_length=6)


PlanTaskV162 = (
    PlanReadingTaskV162
    | PlanDiagnosticTaskV162
    | PlanUnderstandingTaskV162
    | PlanExerciseTaskV162
    | PlanTransferTaskV162
)


class PlanModuleDraftV162(Contract):
    key: str = Field(min_length=1, max_length=40, pattern=r"^[a-zA-Z0-9_-]+$")
    publication_id: str
    focus_fact_ids: list[str] = Field(min_length=1, max_length=12)
    prerequisite_keys: list[str] = Field(default_factory=list, max_length=20)
    steps: list[StepKind] = Field(min_length=1, max_length=5)
    rubric_keys: list[RubricKey] = Field(min_length=1, max_length=6)
    estimated_minutes: int = Field(ge=5, le=240)
    # Null retains the existing source-derived server fallback. It is never a
    # way to hide scenarios/checks or skip the frozen preference checks.
    task: PlanTaskV162 | None = None
    rationale_codes: list[
        Literal[
            "bridge_prerequisite",
            "start_with_architecture",
            "focus_on_function",
            "code_first_preference",
            "smaller_steps",
            "comparison_before_transfer",
            "target_depth",
            "self_reported_context",
        ]
    ] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def no_duplicates(self) -> PlanModuleDraftV162:
        for values in (self.focus_fact_ids, self.prerequisite_keys, self.steps, self.rubric_keys):
            if len(set(values)) != len(values):
                raise ValueError("Duplicate plan module entries")
        return self


class PersonalizedPlanDraftV162(Contract):
    modules: list[PlanModuleDraftV162] = Field(min_length=1, max_length=20)
    uncovered_goal_requirements: list[
        PlanGoalGapDraft | Annotated[str, Field(min_length=1, max_length=2000)]
    ] = Field(default_factory=list, max_length=20)
