"""Closed schemas for learning proposals, observations and paired replay."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract

LearningMode = Literal["project_first", "technology_first"]
LearningIntent = Literal["build_project", "learn_project", "technical_design", "function"]
ActivityKind = Literal["diagnostic", "understanding_check", "exercise", "transfer"]
StepKind = Literal[
    "read_reviewed", "trace_path", "explain_conditions", "contrast_evidence", "transfer_variant"
]
RubricKey = Literal[
    "evidence_and_version", "conditions", "execution_path", "counterexample", "transfer", "runtime"
]


def default_rubric() -> list[RubricKey]:
    return ["evidence_and_version", "conditions"]


class LearningPreferences(Contract):
    learning_mode: LearningMode = "project_first"
    target_depth: Literal["orientation", "explain", "apply", "transfer"] = "explain"
    diagnostics_enabled: bool = True
    checks_enabled: bool = True
    exercises_enabled: bool = True
    preferred_style: Literal["concept_first", "code_first", "comparison_first"] = "concept_first"
    step_size: Literal["small", "normal", "large"] = "normal"
    minutes_per_session: int = Field(default=30, ge=5, le=240)
    self_reported_context: str = Field(default="", max_length=2000)
    goal_version_id: str | None = None


class ProfileUpdate(Contract):
    expected_revision: int = Field(ge=1)
    preferences: LearningPreferences


class PersonalizedPlanInput(Contract):
    namespace_id: str
    funding_id: str
    publication_ids: list[str] = Field(min_length=1, max_length=20)
    learning_intent: LearningIntent
    title: str = Field(min_length=1, max_length=200)
    expected_profile_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=150)
    # API opt-in only. Historical model schemas and omitted-field idempotency
    # fingerprints remain unchanged; see start_personalized_plan.
    task_scope: Literal["question_observables"] | None = None


class PlanTextReference(Contract):
    """An exact, bounded excerpt of the server's authorized text catalog.

    References do not make user goals/self-report into reviewed facts. The server
    rechecks the text, origin, publication, and focus before rendering it as data.
    """

    text_id: str = Field(min_length=1, max_length=180, pattern=r"^[a-zA-Z0-9_.:-]+$")
    quote: str = Field(min_length=1, max_length=600)


class PlanScenarioDraft(Contract):
    kind: Literal[
        "as_given",
        "boundary_equal",
        "boundary_below",
        "boundary_above",
        "repeat_then_return",
        "repeat_then_raise",
    ] = "as_given"
    target: PlanTextReference
    repeat_count: int = Field(default=1, ge=1, le=20)

    @model_validator(mode="after")
    def repeat_only_for_sequences(self) -> PlanScenarioDraft:
        if not self.kind.startswith("repeat_then_") and self.repeat_count != 1:
            raise ValueError("Repeat count applies only to sequence scenarios")
        return self


class PlanTitleReference(PlanTextReference):
    quote: str = Field(min_length=1, max_length=120)


class PlanAcceptanceDraft(Contract):
    rubric_key: RubricKey
    observable: Literal[
        "cite_version",
        "explain_conditions",
        "trace_input_output",
        "contrast_boundary",
        "identify_counterexample",
        "explain_variant",
        "verify_runtime_receipt",
    ]
    targets: list[PlanTextReference] = Field(min_length=1, max_length=4)
    scenario_indexes: list[Annotated[int, Field(ge=0, le=5)]] = Field(
        default_factory=list, max_length=6
    )


class PlanTaskDraft(Contract):
    """No free-form model prose or executable input is accepted for display."""

    kind: Literal["reading", "diagnostic", "understanding_check", "exercise", "transfer"] = (
        "reading"
    )
    title: PlanTitleReference | None = None
    inputs: list[PlanTextReference] = Field(default_factory=list, max_length=8)
    scenarios: list[PlanScenarioDraft] = Field(default_factory=list, max_length=6)
    boundaries: list[PlanTextReference] = Field(default_factory=list, max_length=8)
    acceptance_checks: list[PlanAcceptanceDraft] = Field(default_factory=list, max_length=6)


class PlanGoalGapDraft(Contract):
    requirement: PlanTextReference
    reason: Literal["insufficient_evidence", "outside_source_scope", "runtime_evidence_missing"]
    evidence: list[PlanTextReference] = Field(default_factory=list, max_length=6)


class PlanModuleDraft(Contract):
    key: str = Field(min_length=1, max_length=40, pattern=r"^[a-zA-Z0-9_-]+$")
    publication_id: str
    focus_fact_ids: list[str] = Field(min_length=1, max_length=12)
    prerequisite_keys: list[str] = Field(default_factory=list, max_length=20)
    steps: list[StepKind] = Field(min_length=1, max_length=5)
    rubric_keys: list[RubricKey] = Field(min_length=1, max_length=6)
    estimated_minutes: int = Field(ge=5, le=240)
    task: PlanTaskDraft | None = None
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
    def no_duplicates(self) -> PlanModuleDraft:
        for values in (self.focus_fact_ids, self.prerequisite_keys, self.steps, self.rubric_keys):
            if len(set(values)) != len(values):
                raise ValueError("Duplicate plan module entries")
        return self


class PersonalizedPlanDraft(Contract):
    modules: list[PlanModuleDraft] = Field(min_length=1, max_length=20)
    # Strings remain readable for old providers/records, but are never echoed.
    # New planners select goal anchors and a closed reason code instead.
    uncovered_goal_requirements: list[
        PlanGoalGapDraft | Annotated[str, Field(min_length=1, max_length=2000)]
    ] = Field(default_factory=list, max_length=20)


class ActivityInput(Contract):
    module_id: str
    kind: ActivityKind = "understanding_check"
    prompt_key: Literal[
        "trace_path", "explain_conditions", "counterexample", "transfer_variant"
    ] = "explain_conditions"
    focus_fact_ids: list[str] = Field(default_factory=list, max_length=12)
    rubric_keys: list[RubricKey] = Field(default_factory=default_rubric, min_length=1, max_length=6)
    explicitly_requested: bool = False


class AttemptInput(Contract):
    assistance_mode: Literal["assisted", "independent", "unknown"] = "assisted"
    idempotency_key: str = Field(min_length=1, max_length=150)


class AttemptSubmission(Contract):
    response: str = Field(min_length=1, max_length=10000)
    transfer_notes: str = Field(default="", max_length=6000)
    external_help: Literal["none_declared", "used", "unknown"] = "unknown"
    coding_submission_id: str | None = None


class RubricResult(Contract):
    rubric_item_id: str
    outcome: Literal["demonstrated", "not_yet", "unknown"]
    evidence_excerpt: str = Field(default="", max_length=2000)
    error_type: Literal[
        "none",
        "missing_condition",
        "wrong_path",
        "version_confusion",
        "transfer_gap",
        "not_observed",
    ] = "not_observed"


class ObservationInput(Contract):
    rubric_results: list[RubricResult] = Field(min_length=1, max_length=6)
    observed_behavior: str = Field(min_length=1, max_length=3000)
    evaluator_kind: Literal["self_review", "human_review"] = "self_review"
    supersedes_id: str | None = None


class ObservationWithdraw(Contract):
    expected_revision: int = Field(ge=1)


class AdjustmentInput(Contract):
    expected_revision: int = Field(ge=1)
    observation_ids: list[str] = Field(min_length=1, max_length=20)
    strategy: Literal[
        "smaller_steps", "explain_boundaries", "independent_variant", "transfer_variant"
    ]
    reason: str = Field(min_length=1, max_length=2000)


class ReplayGold(Contract):
    required_fact_ids: list[str] = Field(min_length=1, max_length=40)
    required_action_keys: list[Literal["question", "practice"]] = Field(
        default_factory=list, max_length=2
    )
    preferred_order: Literal["concept_first", "code_first", "comparison_first"] | None = None
    forbidden_text: list[str] = Field(default_factory=list, max_length=20)


class ReplayCase(Contract):
    case_id: str = Field(min_length=1, max_length=100)
    group_id: str = Field(min_length=1, max_length=100)
    repository_family: str = Field(min_length=1, max_length=100)
    user_question_group: str = Field(min_length=1, max_length=100)
    time_partition: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=6000)
    publication_ids: list[str] = Field(min_length=1, max_length=10)
    expected: ReplayGold


class ReplayThresholds(Contract):
    minimum_improvement: float = Field(default=0.0, ge=0, le=1)
    max_quality_regression: float = Field(default=0.0, ge=0, le=0)
    max_latency_ratio: float = Field(default=2.0, ge=1, le=5)
    max_call_ratio: float = Field(default=1.0, ge=1, le=2)
    min_cases: int = Field(default=20, ge=2, le=50)
    min_groups: int = Field(default=10, ge=2, le=50)
    baseline_calibration_id: str | None = None


class ReplayDatasetInput(Contract):
    namespace_id: str
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=40)
    split: Literal["development", "holdout"]
    provenance: Literal["fixture", "human_gold", "development_probe"]
    cases: list[ReplayCase] = Field(min_length=1, max_length=50)
    annotators: list[str] = Field(default_factory=list, max_length=10)
    thresholds: ReplayThresholds


class DatasetAttestationInput(Contract):
    frozen_hash: str = Field(min_length=64, max_length=64)
    decision: Literal["approve", "reject"]
    notes: str = Field(min_length=1, max_length=3000)


class ReplayInput(Contract):
    dataset_id: str
    funding_id: str
    per_arm_call_cap: int = Field(default=2, ge=1, le=20)
    per_arm_request_cap: int = Field(default=10, ge=1, le=100)
    max_research_nodes: int = Field(default=10, ge=2, le=50)
    max_research_depth: int = Field(default=3, ge=1, le=6)
    idempotency_key: str = Field(min_length=1, max_length=150)


class ResearchReplayCaseInput(Contract):
    case_id: str = Field(min_length=1, max_length=100)
    group_id: str = Field(min_length=1, max_length=100)
    user_question_group: str = Field(min_length=1, max_length=100)
    time_partition: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=6000)
    topic_keys: list[str] = Field(min_length=1, max_length=15)
    observation_run_ids: list[str] = Field(min_length=1, max_length=20)
    relevant_repository_ids: list[str] = Field(min_length=1, max_length=100)


class ResearchReplayDatasetInput(Contract):
    namespace_id: str
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=40)
    split: Literal["development", "holdout"]
    provenance: Literal["fixture", "human_gold"]
    cases: list[ResearchReplayCaseInput] = Field(min_length=1, max_length=50)
    annotators: list[str] = Field(default_factory=list, max_length=10)
    thresholds: ReplayThresholds


class TraceCaptureInput(Contract):
    turn_id: str
    signals: list[Literal["reask", "retrieval_gap", "guard_failure"]] = Field(
        min_length=1, max_length=3
    )
    consent: Literal[True]

    @model_validator(mode="after")
    def no_duplicate_signals(self) -> TraceCaptureInput:
        if len(set(self.signals)) != len(self.signals):
            raise ValueError("Duplicate signal types")
        return self


class TypedReplayFocus(Contract):
    project_source_ids: list[str] = Field(default_factory=list, max_length=20)
    topic_keys: list[str] = Field(default_factory=list, max_length=15)


class TypedReplayGold(Contract):
    task_type: (
        Literal[
            "locate_symbol",
            "explain_project_architecture",
            "explain_implementation",
            "teach_concept",
            "compare_projects",
            "verify_claim",
            "discover_projects",
            "practice",
            "plan_learning",
            "import_goal",
            "dispute",
            "inspect_progress",
            "other",
        ]
        | None
    ) = None
    action: (
        Literal[
            "ask_clarification",
            "direct_tool",
            "invoke_tutor",
            "invoke_coding",
            "start_background_job",
            "return_evidence_gap",
        ]
        | None
    ) = None
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    topic_keys: list[str] = Field(default_factory=list, max_length=15)
    reference_resolution: Literal["resolved", "ambiguous", "missing"] = "resolved"
    relevant_publication_ids: list[str] = Field(default_factory=list, max_length=30)
    relevance_complete: bool = False


class TypedReplayCaseInput(Contract):
    case_id: str = Field(min_length=1, max_length=100)
    group_id: str = Field(min_length=1, max_length=100)
    user_question_group: str = Field(min_length=1, max_length=100)
    time_partition: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=6000)
    publication_ids: list[str] = Field(default_factory=list, max_length=30)
    topic_keys: list[str] = Field(default_factory=list, max_length=15)
    task_type: Literal[
        "explain_project_architecture",
        "explain_implementation",
        "teach_concept",
        "compare_projects",
    ] = "explain_implementation"
    focus: TypedReplayFocus = Field(default_factory=TypedReplayFocus)
    expected: TypedReplayGold


class TypedReplayDatasetInput(Contract):
    namespace_id: str
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=40)
    target: Literal["routing_policy", "retrieval_recipe"]
    split: Literal["development", "holdout"]
    provenance: Literal["fixture", "human_gold"]
    cases: list[TypedReplayCaseInput] = Field(min_length=1, max_length=50)
    annotators: list[str] = Field(default_factory=list, max_length=10)
    thresholds: ReplayThresholds
