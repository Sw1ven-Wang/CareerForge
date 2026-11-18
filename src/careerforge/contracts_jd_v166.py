"""Lossless opt-in JD provenance. Model judgments are not learner competence."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, model_validator

from .contracts import Contract

JD_IMPORT_VERSION: Final = "jd-import-1.6.6"
REQUIREMENT_VERSION: Final = "jd-requirements-1.6.6"
RequirementDepth = Literal["了解", "熟悉", "掌握", "精通"]
RequirementModality = Literal["required", "preferred", "bonus", "not_required", "unspecified"]
GroupOperator = Literal["AND", "OR", "NOT"]


class JDSource(Contract):
    kind: str | None = Field(default=None, max_length=120)
    source_label: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=2000)


class JDParagraphInput(Contract):
    paragraph_id: str = Field(min_length=1, max_length=180)
    text: str = Field(min_length=1, max_length=50000)
    index: int | None = Field(default=None, ge=0, strict=True)
    offset: int | None = Field(default=None, ge=0, strict=True)
    text_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class JDImportInput(Contract):
    namespace_id: str = Field(min_length=1, max_length=180)
    jd_id: str = Field(min_length=1, max_length=180)
    raw_text: str = Field(min_length=1, max_length=50000)
    job_title: str | None = Field(default=None, max_length=500)
    source: JDSource | None = None
    source_version: str | None = Field(default=None, max_length=180)
    raw_text_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    raw_text_bytes: int | None = Field(default=None, ge=1, strict=True)
    paragraphs: list[JDParagraphInput] | None = Field(default=None, min_length=1, max_length=1000)


class JDExtractionInput(Contract):
    namespace_id: str = Field(min_length=1, max_length=180)
    funding_id: str = Field(min_length=1, max_length=180)
    jd_revision_id: str = Field(min_length=1, max_length=180)
    idempotency_key: str = Field(min_length=1, max_length=150)
    max_attempts: int | None = Field(default=None, ge=1, le=3, strict=True)


class SourceSpan(Contract):
    quote: str = Field(min_length=1, max_length=2000)
    paragraph_id: str = Field(min_length=1, max_length=180)
    # Absolute, half-open Unicode code-point offsets into untouched raw_text.
    span_start: int = Field(ge=0, strict=True)
    span_end: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def ordered_span(self):
        if self.span_end <= self.span_start:
            raise ValueError("Source spans must be nonempty and half-open")
        return self


class RequirementDraft(SourceSpan):
    key: str = Field(pattern=r"^[a-z0-9_.-]{1,100}$")
    technology: str = Field(min_length=1, max_length=100)
    keywords: list[str] = Field(min_length=1, max_length=8)
    depth: RequirementDepth | None
    depth_basis: Literal["explicit", "inferred", "unspecified"]
    modality: RequirementModality
    group_operator: Literal["SINGLE", "AND", "OR", "NOT"]
    group_key: str | None = Field(default=None, pattern=r"^[a-z0-9_.-]{1,100}$")
    topic_key: str | None = None

    @model_validator(mode="after")
    def qualifier_consistency(self):
        if (self.depth is None) != (self.depth_basis == "unspecified"):
            raise ValueError("Unknown depth must stay null with unspecified basis")
        if (self.group_operator == "SINGLE") != (self.group_key is None):
            raise ValueError("Grouped requirements need an explicit group key")
        if self.modality == "not_required" and self.group_operator != "NOT":
            raise ValueError("An explicit non-requirement needs its NOT group")
        return self


class RequirementGroupDraft(SourceSpan):
    key: str = Field(pattern=r"^[a-z0-9_.-]{1,100}$")
    operator: GroupOperator
    member_keys: list[str] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def arity_and_uniqueness(self):
        if len(self.member_keys) != len(set(self.member_keys)):
            raise ValueError("Duplicate logical group members")
        if self.operator == "NOT" and len(self.member_keys) != 1:
            raise ValueError("NOT is unary")
        if self.operator != "NOT" and len(self.member_keys) < 2:
            raise ValueError("AND and OR need at least two members")
        return self


class RequirementExtractionOutput(Contract):
    requirements: list[RequirementDraft] = Field(default_factory=list, max_length=30)
    groups: list[RequirementGroupDraft] = Field(default_factory=list, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def unique_keys(self):
        keys = [r.key for r in self.requirements] + [g.key for g in self.groups]
        if len(keys) != len(set(keys)):
            raise ValueError("Requirement and group keys must be globally unique")
        return self


class RequirementGroup(Contract):
    group_id: str = Field(pattern=r"^group-[a-f0-9]{24}$")
    operator: GroupOperator
    member_ids: list[str] = Field(min_length=1, max_length=30)
    quote: str
    paragraph_id: str
    span_start: int
    span_end: int


class ProvenanceRequirement(SourceSpan):
    requirement_contract_version: Literal["jd-requirements-1.6.6"] = REQUIREMENT_VERSION
    key: str = Field(pattern=r"^req-[a-f0-9]{24}$")
    requirement_id: str = Field(pattern=r"^req-[a-f0-9]{24}$")
    technology: str = Field(min_length=1, max_length=100)
    keywords: list[str] = Field(min_length=1, max_length=8)
    depth: RequirementDepth | None
    depth_basis: Literal["explicit", "inferred", "unspecified"]
    modality: RequirementModality
    group_operator: Literal["SINGLE", "AND", "OR", "NOT"]
    group_id: str | None
    group_path: list[RequirementGroup] = Field(default_factory=list, max_length=30)
    topic_key: str | None = None
    jd_revision_id: str
    raw_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    paragraph_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    user_mastery_unchanged: Literal[True] = True

    @model_validator(mode="after")
    def stable_identity_and_qualifiers(self):
        if self.key != self.requirement_id:
            raise ValueError("Requirement key and versioned identity differ")
        if (self.depth is None) != (self.depth_basis == "unspecified"):
            raise ValueError("Unknown depth must remain unspecified")
        if (self.group_operator == "SINGLE") != (self.group_id is None):
            raise ValueError("Requirement group identity is missing")
        if self.modality == "not_required" and self.group_operator != "NOT":
            raise ValueError("A non-requirement cannot become mandatory")
        if self.group_path:
            leaf = self.group_path[-1]
            if leaf.group_id != self.group_id or leaf.operator != self.group_operator:
                raise ValueError("Requirement group path disagrees with direct parent")
            if self.requirement_id not in leaf.member_ids:
                raise ValueError("Requirement is absent from its direct group")
            for parent, child in zip(self.group_path, self.group_path[1:], strict=False):
                if child.group_id not in parent.member_ids:
                    raise ValueError("Disconnected logical group path")
        elif self.group_id is not None:
            raise ValueError("Grouped requirement is missing its complete path")
        return self
