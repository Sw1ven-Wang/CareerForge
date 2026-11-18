"""Typed administrator-reviewed changes; model proposals cannot execute governance."""

import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract


class TermAlias(Contract):
    term: str = Field(min_length=1, max_length=150)
    language: Literal["zh", "en", "other"] = "other"
    context: str = Field(default="", max_length=500)


class RelationProposal(Contract):
    target_key: str = Field(min_length=2, max_length=120)
    relation: Literal["broader", "prerequisite", "related"]


class FacetPrecondition(Contract):
    predicate: Literal["origin_kind_is", "unit_type_is", "path_present"]
    operand: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def registered_operands(self):
        if self.predicate == "origin_kind_is" and self.operand not in {"repository", "concept"}:
            raise ValueError("origin kind must be a registered source kind")
        if self.predicate == "unit_type_is" and self.operand not in {
            "concept",
            "overview",
            "architecture",
            "implementation",
            "function",
            "tradeoff",
            "testing",
        }:
            raise ValueError("unit type must be registered")
        if self.predicate == "path_present" and (
            PurePosixPath(self.operand).is_absolute()
            or ".." in PurePosixPath(self.operand).parts
            or "\\" in self.operand
        ):
            raise ValueError("path predicate must be a relative source path")
        return self


class GovernedFacet(Contract):
    question: str = Field(min_length=1, max_length=500)
    required: bool = False
    applicability: Literal["always", "requires_claim"] = "always"
    value_type: Literal["string", "number", "boolean", "object", "enum"] = "string"
    unit: str | None = Field(default=None, min_length=1, max_length=100)
    preconditions: list[FacetPrecondition] = Field(default_factory=list, max_length=10)
    absent_value_policy: Literal["unknown", "unsupported_when_proven"] = "unknown"
    enum_values: list[str] = Field(default_factory=list, max_length=40)
    object_fields: dict[str, Literal["string", "number", "boolean"]] = Field(
        default_factory=dict, max_length=20
    )

    @model_validator(mode="after")
    def typed_shape(self):
        if self.preconditions and self.applicability == "always":
            raise ValueError("conditional preconditions require requires_claim applicability")
        if self.unit is not None and self.value_type != "number":
            raise ValueError("only numerical values can declare a unit")
        if len(self.enum_values) != len(set(self.enum_values)) or any(
            not term or len(term) > 200 for term in self.enum_values
        ):
            raise ValueError("enum values must be distinct bounded nonempty terms")
        if any(not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,79}", key) for key in self.object_fields):
            raise ValueError("object fields must have registered bounded identifiers")
        if self.value_type == "enum" and not self.enum_values:
            raise ValueError("enum requires explicit allowed values")
        if self.value_type == "object" and not self.object_fields:
            raise ValueError(
                "object requires explicit scalar fields; arbitrary objects are not allowed"
            )
        return self


class GovernedTopic(Contract):
    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,119}$")
    title: str = Field(min_length=1, max_length=200)
    definition: str = Field(min_length=4, max_length=4000)
    topic_kind: Literal[
        "domain", "language", "framework", "concept", "mechanism", "engineering_practice"
    ]
    aliases: list[TermAlias] = Field(default_factory=list, max_length=50)
    relations: list[RelationProposal] = Field(default_factory=list, max_length=30)
    facets: dict[str, GovernedFacet] = Field(default_factory=dict, max_length=30)


class TaxonomyProposalInput(Contract):
    namespace_id: str
    operation: Literal["create", "update", "schema", "merge", "split"]
    definitions: list[GovernedTopic] = Field(min_length=1, max_length=10)
    source_topic_versions: dict[str, int] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    rationale: str = Field(min_length=4, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=150)

    @model_validator(mode="after")
    def shape(self):
        if len({x.key for x in self.definitions}) != len(self.definitions):
            raise ValueError("duplicate topic definitions")
        if self.operation in {"update", "schema"} and (
            len(self.definitions) != 1 or len(self.source_topic_versions) != 1
        ):
            raise ValueError("a revision updates one explicit topic version")
        if self.operation == "merge" and (
            len(self.definitions) != 1 or len(self.source_topic_versions) < 2
        ):
            raise ValueError("merge requires at least two sources and one new identity")
        if self.operation == "split" and (
            len(self.definitions) < 2 or len(self.source_topic_versions) != 1
        ):
            raise ValueError("split requires one source and at least two new identities")
        if self.operation == "create" and self.source_topic_versions:
            raise ValueError("create has no predecessor")
        return self


class TaxonomyDecisionInput(Contract):
    expected_revision: int = Field(ge=1)
    expected_release_revision: int = Field(ge=0)
    approve: bool
    reason: str = Field(min_length=4, max_length=4000)
    equivalence_confirmed: bool = False


class UpdatePlanInput(Contract):
    namespace_id: str
    old_source_id: str
    new_source_id: str


class UpdateExecutionInput(Contract):
    expected_revision: int = Field(ge=1)
    funding_id: str
    call_cap: int = Field(default=10, ge=5, le=100)


class MaintenanceInput(Contract):
    namespace_id: str
    source_id: str
    interval_seconds: int = Field(default=86400, ge=3600, le=2592000)
    allow_model_calls: bool = False
    funding_id: str | None = None
    call_cap: int = Field(default=10, ge=5, le=100)


class MaintenanceToggle(Contract):
    expected_revision: int = Field(ge=1)
    enabled: bool
