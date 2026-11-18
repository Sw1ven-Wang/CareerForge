"""Versioned, strict inputs and outputs. All identifiers are revalidated server-side."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PrivateAttr,
    field_validator,
    model_serializer,
    model_validator,
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class TaskType(StrEnum):
    LOCATE = "locate_symbol"
    ARCHITECTURE = "explain_project_architecture"
    IMPLEMENTATION = "explain_implementation"
    CONCEPT = "teach_concept"
    COMPARE = "compare_projects"
    VERIFY = "verify_claim"
    DISCOVER = "discover_projects"
    PRACTICE = "practice"
    PLAN = "plan_learning"
    IMPORT = "import_goal"
    DISPUTE = "dispute"
    PROGRESS = "inspect_progress"
    OTHER = "other"


IntentAction = Literal[
    "direct_tool",
    "retrieve_and_tutor",
    "knowledge_build",
    "coding",
    "ask_clarification",
    "record_gap",
]
CoordinatorAction = Literal[
    "direct_tool",
    "invoke_tutor",
    "start_background_job",
    "invoke_coding",
    "ask_clarification",
    "return_evidence_gap",
]
RetrievalChannel = Literal[
    "topic_relation", "ast_symbol", "postgres_fts", "pgvector", "source_read"
]


class IntentDecision(Contract):
    interpreted_intent: TaskType
    # Retained for legacy consumers and display. New routing decisions carry
    # their explicit retrieval constraint separately from inferred topics.
    topic_keys: list[str] = Field(default_factory=list, max_length=10)
    retrieval_topic_keys: list[str] | None = Field(default=None, max_length=10)
    topic_hints: list[str] = Field(default_factory=list, max_length=10)
    retrieval_scope_version: Literal["explicit_scope_v2"] | None = None
    project_source_ids: list[str] = Field(default_factory=list, max_length=10)
    code_focus_ref: str | None = None
    reference_resolution: Literal["resolved", "ambiguous", "missing"] = "resolved"
    confidence: float = Field(ge=0, le=1)
    proposed_next_action: IntentAction
    rationale_codes: list[str] = Field(default_factory=list, max_length=5)


class OrchestrationDecision(Contract):
    action: CoordinatorAction
    trigger_code: str
    principal_id: str
    authorized_namespace_refs: list[str]
    focus_revision: int
    task_type: TaskType
    source_version_refs: list[str]
    topic_keys: list[str]
    policy_version: str
    task_contract_ref: str | None = None
    budget_reservation_ref: str | None = None


class RetrievalTaskSpec(Contract):
    turn_id: str
    task_type: TaskType
    user_question: str = Field(max_length=10000)
    focus_revision: int = Field(ge=1)
    # This legacy field remains a hard filter. Never replace it with inferred
    # topics, including when an authorized fallback or rewrite finds no hits.
    topic_keys: list[str] = Field(default_factory=list)
    topic_hints: list[str] = Field(default_factory=list, max_length=30)
    retrieval_scope_version: Literal["explicit_scope_v2"] = "explicit_scope_v2"
    project_source_ids: list[str] = Field(default_factory=list)
    authorized_namespace_refs: list[str] = Field(default_factory=list)
    evidence_policy: Literal["published_only", "direct_source_observation"] = "published_only"
    freshness_policy: Literal["selection_manifest", "explicit_pinned"] = "selection_manifest"
    pinned_publication_ids: list[str] = Field(default_factory=list)
    required_facets: list[str] = Field(default_factory=list)
    language_hints: list[str] = Field(default_factory=list)
    max_project_samples: int = Field(default=3, ge=1, le=10)
    # Historical wire name: enforced as canonical knowledge-unit UTF-8 bytes,
    # not tokenizer tokens. Zero explicitly requests no reviewed evidence.
    max_evidence_tokens: int = Field(default=12000, ge=0, le=40000)
    per_project_limit: int = Field(default=3, ge=1, le=10)
    latency_budget_ms: int = Field(default=5000, ge=100, le=60000)
    selected_routing_policy_version: str = "builtin-1.5"


class RetrievalPlan(Contract):
    allowed_channels: list[RetrievalChannel]
    channel_order: list[str]
    query_variants: list[str]
    fusion: Literal["none", "rrf"]
    rerank: Literal["enabled", "disabled"]
    group_by_project: bool
    per_project_limit: int
    recipe_version: str
    stop_and_fallback_rules: list[str]


class QuerySpec(Contract):
    channel: Literal[
        "repository", "code", "documentation", "issues", "pull_requests", "releases"
    ] = "repository"
    query: str = Field(min_length=2, max_length=180)
    question: str = Field(min_length=2, max_length=600)
    topic_keys: list[str] = Field(max_length=10)


class QueryPlan(Contract):
    queries: list[QuerySpec] = Field(default_factory=list, max_length=3)
    stop_reason: str | None = None


class ExtractedRequirement(Contract):
    key: str = Field(pattern=r"^[a-z0-9_.-]{1,100}$")
    technology: str = Field(min_length=1, max_length=100)
    keywords: list[str] = Field(min_length=1, max_length=8)
    depth: Literal["了解", "熟悉", "掌握"]
    depth_basis: Literal["explicit", "inferred"]
    quote: str = Field(min_length=2, max_length=1000)
    topic_key: str | None = None


class ExtractOutput(Contract):
    requirements: list[ExtractedRequirement] = Field(min_length=1, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=10)


class ToolCall(Contract):
    id: str
    name: str
    arguments: dict[str, Any]


class ModelOutput(Contract):
    text: str = ""
    structured: dict[str, Any] | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, Any] = Field(default_factory=dict)
    synthetic: bool = False
    provider_response_id: str | None = None


class ReadReference(Contract):
    read_id: str
    quote: str = Field(min_length=4, max_length=2600)


class ClaimDraft(Contract):
    key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,80}$")
    statement: str = Field(min_length=4, max_length=2000)
    conditions: list[str] = Field(default_factory=list, max_length=8)
    kind: Literal[
        "source_fact", "documented_intent", "analytical_inference", "runtime_observation"
    ] = "source_fact"
    support: list[ReadReference] = Field(min_length=1, max_length=6)
    counterevidence: list[ReadReference] = Field(default_factory=list, max_length=6)


class TopicAssociation(Contract):
    topic_key: str
    relation: Literal["implements", "integrates", "illustrates", "mentions", "defines"]


class FacetDraft(Contract):
    topic_key: str
    facet_key: str
    value_status: Literal["known", "unknown", "not_applicable", "conflicting"]
    value: JsonValue
    claim_keys: list[str] = Field(default_factory=list)
    applicability_reason: str

    @field_validator("value")
    @classmethod
    def bounded_json_value(cls, value: JsonValue) -> JsonValue:
        # Keep numeric/boolean/object values lossless; the frozen Facet schema
        # makes the final type/enum/applicability decision in KnowledgeService.
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (ValueError, OverflowError, RecursionError) as exc:
            raise ValueError("Facet values must be finite JSON") from exc
        if len(encoded.encode("utf-8")) > 20000:
            raise ValueError("Facet value exceeds its bounded JSON contract")
        return value


class UnitDraft(Contract):
    subject_key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,160}$")
    unit_type: Literal[
        "concept", "overview", "architecture", "implementation", "function", "tradeoff", "testing"
    ]
    title: str = Field(min_length=1, max_length=200)
    applicability: str = Field(default="default", max_length=600)
    claims: list[ClaimDraft] = Field(min_length=1, max_length=12)
    topics: list[TopicAssociation] = Field(min_length=1, max_length=10)
    facets: list[FacetDraft] = Field(default_factory=list, max_length=30)
    dependency_paths: list[str] = Field(default_factory=list, max_length=100)
    limitations: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def unique_keys(self):
        keys = [c.key for c in self.claims]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate claim keys")
        if any(not set(f.claim_keys) <= set(keys) for f in self.facets):
            raise ValueError("facet references unknown claim")
        return self


class AnalysisOutput(Contract):
    units: list[UnitDraft] = Field(min_length=1, max_length=5)
    unanswered_questions: list[str] = Field(default_factory=list, max_length=10)


class ClaimReview(Contract):
    claim_id: str
    outcome: Literal[
        "supported_within_scope", "partially_supported", "contradicted", "insufficient"
    ]
    independent_reads: list[ReadReference] = Field(default_factory=list, max_length=8)
    counterevidence: list[ReadReference] = Field(default_factory=list, max_length=8)
    boundary: str


class ReviewOutput(Contract):
    claim_decisions: list[ClaimReview]
    accepted_topic_link_ids: list[str]
    accepted_facet_ids: list[str]
    blocking_issues: list[str]
    decision: Literal["accept_scoped", "needs_evidence", "reject"]


class CoverageOutput(Contract):
    all_facts_bound: bool
    conditions_preserved: bool
    uncovered_elements: list[str]
    unsupported_titles: list[str]
    rationale_codes: list[str]


class AnswerPart(Contract):
    kind: Literal[
        "repository_fact",
        "concept_fact",
        "comparison",
        "learning_action",
        "evidence_gap",
        "raw_source_observation",
    ]
    text: str = Field(min_length=1, max_length=5000)
    fact_atom_ids: list[str] = Field(default_factory=list)
    publication_ids: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    transformation: Literal["exact", "paraphrase"] = "exact"
    style: Literal["paragraph", "code_first", "table_cell", "diagram_edge"] = "paragraph"


class AnswerOutput(Contract):
    blocks: list[AnswerPart] = Field(min_length=1, max_length=12)


class SemanticCheck(Contract):
    supported: bool
    conditions_preserved: bool
    quantifiers_preserved: bool
    no_new_project_facts: bool
    reasons: list[str]


class MechanismReadTarget(Contract):
    path: str = Field(min_length=1, max_length=1000)
    symbol: str = Field(min_length=1, max_length=200)
    start_line: int = Field(ge=1, strict=True)
    end_line: int = Field(ge=1, strict=True)
    purpose: Literal["registration", "dispatch", "result", "boundary"]

    @model_validator(mode="after")
    def bounded_window(self):
        if not 0 <= self.end_line - self.start_line < 30:
            raise ValueError("Mechanism read windows must contain 1 to 30 lines")
        return self


class MechanismEdgeTarget(Contract):
    key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,80}$")
    caller: MechanismReadTarget
    callee: MechanismReadTarget

    @model_validator(mode="after")
    def cross_file(self):
        if self.caller.path == self.callee.path:
            raise ValueError("A mechanism call edge must cross two frozen files")
        return self


class KnowledgeBuildInput(Contract):
    # Server-only fence for a legacy operation spanning admission transactions.
    # It is never accepted from JSON or included in schemas/manifests/hashes.
    _admission_privacy_generation: int | None = PrivateAttr(default=None)

    namespace_id: str
    funding_id: str
    question: str = Field(min_length=1, max_length=6000)
    repository_url: str | None = None
    source_id: str | None = None
    topic_keys: list[str] = Field(default_factory=list, max_length=15)
    jd_text: str | None = Field(default=None, max_length=50000)
    # A separately imported immutable revision opts into lossless JD provenance.
    jd_revision_id: str | None = Field(default=None, min_length=1, max_length=180)
    candidate_selection_version: Literal["candidate-selection-1.6.6"] | None = None
    goal_version_id: str | None = Field(default=None, min_length=1, max_length=180)
    # New runs freeze this explicitly in their manifest. None remains readable
    # for old callers; the service never upgrades an already queued old run.
    research_contract_version: Literal["research-task-1.6.3"] | None = None
    research_scope: Literal["project", "focused", "concept"] | None = None
    mechanism_read_targets: list[MechanismReadTarget] = Field(default_factory=list, max_length=4)
    auto_evidence_retry: bool = Field(default=False, strict=True)
    coverage_guided_analysis: bool = Field(default=False, strict=True)
    mechanism_edge_targets: list[MechanismEdgeTarget] = Field(default_factory=list, max_length=3)
    discover_mechanisms: bool = Field(default=False, strict=True)
    required_dimensions: list[
        Literal[
            "purpose",
            "project_structure",
            "module_responsibilities",
            "call_flow",
            "implementation",
            "boundaries",
            "tradeoffs",
        ]
    ] = Field(default_factory=list, max_length=7)
    build_wall_time_contract: Literal["unlimited-build-time-v1"] | None = None
    duration_minutes: int = Field(default=30, ge=1, le=10080, strict=True)
    planning_call_seconds: int = Field(default=120, ge=1, le=3600, strict=True)
    call_cap: int = Field(default=24, ge=1, le=100)
    max_nodes: int = Field(default=12, ge=1, le=40)
    max_depth: int = Field(default=3, ge=1, le=5)
    max_projects: int = Field(default=3, ge=1, le=5)
    max_units_per_project: int = Field(default=2, ge=1, le=5)
    github_request_cap: int = Field(default=30, ge=1, le=120)
    idempotency_key: str = Field(min_length=1, max_length=150)

    @model_serializer(mode="wrap")
    def serialize_build_time_choice(self, handler):
        # Absent/default opt-in preserves pre-contract JSON and idempotency hashes.
        data = handler(self)
        if self.build_wall_time_contract is None:
            data.pop("build_wall_time_contract", None)
        return data


class ImportSourceInput(Contract):
    namespace_id: str
    repository_key: str = Field(min_length=1, max_length=500)
    origin_kind: Literal["repository", "concept"] = "repository"
    commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    license_id: str = Field(min_length=1, max_length=100)
    files: dict[str, str]
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    rights_confirmed: bool = False

    @model_validator(mode="after")
    def commit_required(self):
        if self.origin_kind == "repository" and not self.commit_sha:
            raise ValueError("repository needs fixed commit")
        return self


class TurnInput(Contract):
    question: str = Field(min_length=1, max_length=10000)
    expected_revision: int = Field(ge=1)
    funding_id: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=150)


class FeedbackInput(Contract):
    turn_id: str
    signal: Literal["explicit_feedback", "correction", "reask", "navigation", "exercise_result"]
    content: str = Field(min_length=1, max_length=3000)
    consent: bool


class PolicyPatch(Contract):
    target: Literal["teaching_skill", "routing_policy", "retrieval_recipe", "research_heuristic"]
    content: dict[str, Any]
    experience_ids: list[str]
    training_group_ids: list[str]


class PatchEdit(Contract):
    path: str
    before: str | None
    after: str | None


class CodingReply(Contract):
    answer: str = Field(max_length=6000)
    citation_ids: list[str] = Field(default_factory=list)
    edits: list[PatchEdit] = Field(default_factory=list, max_length=2)
    commands: list[list[str]] = Field(default_factory=list, max_length=3)
