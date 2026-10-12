"""CareerForge M1/M2 cross-module *reference* Pydantic v2 models.

Not a drop-in migration for the user's existing V1 repo. Runtime DB and API adapters
must be reconciled against the current schema and endpoint contracts first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, field_validator, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SourceInfo(ContractModel):
    source_capture_id: str
    provider: str
    ingest_method: Literal["official_api", "public_page", "json_ld", "manual_import"]
    raw_snapshot_id: str
    source_url: str | None = None
    external_posting_id: str | None = None
    retrieved_at: datetime
    read_status: Literal["full_text", "partial_text", "metadata_only", "unavailable"]

    @field_validator("retrieved_at")
    @classmethod
    def require_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrieved_at must carry timezone information")
        return value.astimezone(UTC)


class JobMeta(ContractModel):
    title_raw: str
    employer_name: str | None = None
    role_family: str | None = None
    employment_type: str | None = None
    internship_type: str | None = None
    locations: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    provider_updated_at: datetime | None = None
    salary_raw: str | None = None
    published_at_semantics: Literal[
        "first_published", "last_published", "date_posted", "unknown"
    ] = "unknown"

    @model_validator(mode="after")
    def published_date_semantics(self) -> JobMeta:
        if self.published_at is None and self.published_at_semantics != "unknown":
            raise ValueError("cannot assign date semantics without a published_at date")
        return self

    @field_validator("published_at", "provider_updated_at")
    @classmethod
    def require_aware_times(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("job timestamp must carry timezone information")
        return value.astimezone(UTC)


class DocumentSection(ContractModel):
    section_id: str
    kind: Literal["responsibility", "qualification", "preferred", "benefit", "other"]
    start: NonNegativeInt
    end: NonNegativeInt
    text: str

    @model_validator(mode="after")
    def check_local_span(self) -> DocumentSection:
        if self.end <= self.start:
            raise ValueError("section end must be greater than start")
        return self


class JobDocument(ContractModel):
    canonical_text: str
    sections: list[DocumentSection]

    @model_validator(mode="after")
    def check_sections(self) -> JobDocument:
        seen: set[str] = set()
        for s in self.sections:
            if s.section_id in seen:
                raise ValueError("duplicate section_id")
            seen.add(s.section_id)
            if s.end > len(self.canonical_text):
                raise ValueError("section span out of bounds")
            if self.canonical_text[s.start : s.end] != s.text:
                raise ValueError("section text mismatch")
        return self


class JobQuality(ContractModel):
    normalization_status: Literal["ready", "partial", "blocked"]
    normalizer_version: str
    dedupe_status: Literal["unique", "confirmed_duplicate", "pending_review"]
    dedupe_group_id: str | None = None
    issues: list[str] = Field(default_factory=list)


class JobPostingSnapshotV1(ContractModel):
    schema_version: Literal["careerforge.job_snapshot.v1"]
    posting_id: str
    job_snapshot_id: str
    source: SourceInfo
    job: JobMeta
    document: JobDocument
    quality: JobQuality

    @model_validator(mode="after")
    def ensure_ready_contains_text(self) -> JobPostingSnapshotV1:
        if (
            self.source.read_status == "full_text"
            and self.quality.normalization_status == "ready"
            and not self.document.canonical_text.strip()
        ):
            raise ValueError("full-text ready job has empty canonical text")
        return self


class ConceptExpr(ContractModel):
    type: Literal["concept"] = "concept"
    concept_id: str
    concept_kind: Literal["technology", "capability", "knowledge", "other"]


class ConstraintExpr(ContractModel):
    type: Literal["constraint"] = "constraint"
    field: str
    comparator: Literal["eq", "gte", "lte", "in"]
    value: str | int | float | list[str]
    unit: str | None = None


class BooleanExpr(ContractModel):
    type: Literal["all_of", "any_of"]
    children: list[RequirementExpr]

    @model_validator(mode="after")
    def require_multiple_children(self) -> BooleanExpr:
        if len(self.children) < 2:
            raise ValueError("boolean requirements must have >= 2 children")
        return self


RequirementExpr = Annotated[ConceptExpr | ConstraintExpr | BooleanExpr, Field(discriminator="type")]
BooleanExpr.model_rebuild(_types_namespace={"RequirementExpr": RequirementExpr})


class EvidenceSpan(ContractModel):
    raw_snapshot_id: str
    job_snapshot_id: str
    section_id: str
    start: NonNegativeInt
    end: NonNegativeInt
    quote: str

    @model_validator(mode="after")
    def check_local_span(self) -> EvidenceSpan:
        if self.end <= self.start:
            raise ValueError("evidence end must exceed start")
        if not self.quote:
            raise ValueError("evidence quote cannot be empty")
        return self


class RequirementFactV1(ContractModel):
    schema_version: Literal["careerforge.requirement.v1"]
    requirement_id: str
    job_snapshot_id: str
    requirement_kind: Literal[
        "technical_skill", "engineering_capability", "knowledge", "eligibility", "other"
    ]
    importance: Literal["required", "preferred", "responsibility", "unknown"]
    origin: Literal["explicit", "inferred"]
    expression: RequirementExpr
    evidence: EvidenceSpan
    review_status: Literal["auto_extracted", "pending_review", "confirmed", "rejected"]
    extractor_version: str
    taxonomy_version: str


class CountStats(ContractModel):
    count: NonNegativeInt
    denominator: NonNegativeInt

    @model_validator(mode="after")
    def valid_ratio(self) -> CountStats:
        if self.count > self.denominator:
            raise ValueError("numerator exceeds denominator")
        return self


class DemandItem(ContractModel):
    demand_id: str
    demand_unit: Literal["expression_group", "concept"]
    expression_signature: str
    concept_id: str | None = None
    required_solo: CountStats
    required_alternative: CountStats
    preferred: CountStats
    responsibility: CountStats
    supporting_requirement_ids: list[str]

    @model_validator(mode="after")
    def ensure_dimension_key(self) -> DemandItem:
        if self.demand_unit == "concept" and not self.concept_id:
            raise ValueError("concept-unit demand requires concept_id")
        if self.demand_unit == "expression_group" and self.concept_id is not None:
            raise ValueError("expression-group demand must not include concept_id")
        return self


class ReportScope(ContractModel):
    role_families: list[str] = Field(default_factory=list)
    employment_types: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)


class DemandReportV1(ContractModel):
    schema_version: Literal["careerforge.demand_report.v1"]
    report_id: str
    goal_version_id: str
    scope: ReportScope
    included_job_snapshot_ids: list[str]
    excluded_job_snapshot_ids: list[str] = Field(default_factory=list)
    excluded_reasons: dict[str, str] = Field(default_factory=dict)
    eligibility_requirement_ids: list[str] = Field(default_factory=list)
    denominator: NonNegativeInt
    taxonomy_version: str
    extractor_version: str
    aggregation_version: str
    demands: list[DemandItem]

    @model_validator(mode="after")
    def check_denominator(self) -> DemandReportV1:
        if self.denominator != len(set(self.included_job_snapshot_ids)):
            raise ValueError("report denominator must match unique included snapshots")
        if len(self.included_job_snapshot_ids) != len(set(self.included_job_snapshot_ids)):
            raise ValueError("report contains repeated job snapshots")
        if not set(self.excluded_reasons).issubset(set(self.excluded_job_snapshot_ids)):
            raise ValueError("excluded reason references unlisted snapshot")
        for item in self.demands:
            for stats in (
                item.required_solo,
                item.required_alternative,
                item.preferred,
                item.responsibility,
            ):
                if stats.denominator != self.denominator:
                    raise ValueError("demand denominator inconsistent")
        return self


class EligibilityCheck(ContractModel):
    field: str
    status: Literal["matched", "unmet", "unknown"]
    source_requirement_ids: list[str] = Field(default_factory=list)
    action: Literal["none", "ask_user", "review"] = "none"


class UserConstraints(ContractModel):
    hours_per_week: float | None = Field(default=None, ge=0)
    preferred_languages: list[str] = Field(default_factory=list)
    target_date: str | None = None  # project may use a native date type


class PlanningPolicy(ContractModel):
    preserve_unknown: bool = True
    explicit_requirements_only: bool = True
    require_source_evidence: bool = True


class PlanningContextV1(ContractModel):
    schema_version: Literal["careerforge.planning_context.v1"]
    planning_context_id: str
    goal_version_id: str
    demand_report_id: str
    selected_demand_ids: list[str]
    personal_evidence_version_ids: list[str]
    eligibility_checks: list[EligibilityCheck] = Field(default_factory=list)
    user_constraints: UserConstraints
    planning_policy: PlanningPolicy = Field(default_factory=PlanningPolicy)
    input_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def validate_requirement_against_snapshot(
    requirement: RequirementFactV1,
    snapshot: JobPostingSnapshotV1,
) -> None:
    """Validate exact evidence against immutable canonical text (Python codepoints)."""
    ev = requirement.evidence
    if requirement.job_snapshot_id != snapshot.job_snapshot_id:
        raise ValueError("requirement points to a different job snapshot")
    if ev.job_snapshot_id != snapshot.job_snapshot_id:
        raise ValueError("evidence points to a different job snapshot")
    if ev.raw_snapshot_id != snapshot.source.raw_snapshot_id:
        raise ValueError("evidence raw snapshot mismatch")
    sec = next((x for x in snapshot.document.sections if x.section_id == ev.section_id), None)
    if sec is None or ev.start < sec.start or ev.end > sec.end:
        raise ValueError("evidence span not contained within referenced section")
    if snapshot.document.canonical_text[ev.start : ev.end] != ev.quote:
        raise ValueError("evidence quote does not match canonical text")
    if (
        snapshot.source.read_status != "full_text"
        or snapshot.quality.normalization_status != "ready"
    ):
        raise ValueError("source not eligible for confirmed full-text extraction")


class CaptureImport(ContractModel):
    job_category: str | None = Field(default=None, max_length=100)
    source_category: str | None = Field(default=None, max_length=100)
    provider: Literal["manual_import", "greenhouse", "lever", "ashby", "json_ld"] = "manual_import"
    source_url: str | None = None
    board_scope: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=500)
    employer_name: str | None = Field(default=None, max_length=500)
    raw_payload: str = Field(max_length=1000000)
    read_status: Literal["full_text", "partial_text", "metadata_only", "unavailable"] = "full_text"
    http_status: int | None = Field(default=None, ge=100, le=599)
    usage_policy_status: Literal["allowed", "restricted", "unknown"] = "unknown"


class FactRevision(ContractModel):
    expected_fact_id: str
    correction: RequirementFactV1
    reason: str = Field(min_length=1, max_length=2000)


class ContextInput(ContractModel):
    @field_validator("goal_version_id", "demand_report_id")
    @classmethod
    def valid_identifier(cls, value: str) -> str:
        from uuid import UUID

        return str(UUID(value))

    @field_validator("personal_evidence_version_ids")
    @classmethod
    def valid_evidence_identifiers(cls, values: list[str]) -> list[str]:
        from uuid import UUID

        return [str(UUID(value)) for value in values]

    goal_version_id: str
    demand_report_id: str
    selected_demand_ids: list[str] = Field(default_factory=list, max_length=20)
    personal_evidence_version_ids: list[str] = Field(default_factory=list, max_length=20)
    user_constraints: UserConstraints = Field(default_factory=UserConstraints)


class ContextAlignmentInput(ContractModel):
    planning_context_id: str
