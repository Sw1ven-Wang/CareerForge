"""Explicit r3 evaluation inputs. These labels are evaluator-only, never prompts."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract
from .contracts_learning_ext import ReplayThresholds, TypedReplayFocus


class RetrievalFactLabel(Contract):
    publication_id: str
    unit_version_id: str
    source_id: str
    commit: str
    fact_atom_id: str
    relevant: bool
    # Explicit complete support, including counterevidence, not just a matching ID.
    support_refs: list[str] = Field(min_length=1, max_length=100)
    counterevidence_refs: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_references(self) -> RetrievalFactLabel:
        for refs in (self.support_refs, self.counterevidence_refs):
            if len(refs) != len(set(refs)):
                raise ValueError("Duplicate evidence reference")
        return self


class RetrievalRelevanceGold(Contract):
    labels: list[RetrievalFactLabel] = Field(min_length=1, max_length=1000)
    complete_candidate_pool: Literal[True]


class RetrievalLabelProvenance(Contract):
    kind: Literal["synthetic_fixture", "independent_human"]
    protocol: Literal["complete-fact-evidence-version-relevance-1"]
    # An external annotation record is identified, not fetched or sent to models.
    record_reference: str = Field(min_length=1, max_length=500)
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RetrievalR3ReplayCase(Contract):
    case_id: str = Field(min_length=1, max_length=100)
    group_id: str = Field(min_length=1, max_length=100)
    user_question_group: str = Field(min_length=1, max_length=100)
    time_partition: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=6000)
    publication_ids: list[str] = Field(min_length=1, max_length=30)
    topic_keys: list[str] = Field(default_factory=list, max_length=15)
    task_type: Literal[
        "explain_project_architecture",
        "explain_implementation",
        "teach_concept",
        "compare_projects",
    ] = "explain_implementation"
    focus: TypedReplayFocus = Field(default_factory=TypedReplayFocus)
    expected: RetrievalRelevanceGold


class RetrievalR3ReplayDatasetInput(Contract):
    # Required, so omitted historical markers can never opt into r3 semantics.
    execution_contract: Literal["bounded-fact-typed-execution-1.6.6-r3-v1"]
    namespace_id: str
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=40)
    target: Literal["retrieval_recipe"] = "retrieval_recipe"
    split: Literal["development", "holdout"]
    provenance: Literal["fixture", "human_gold"]
    label_provenance: RetrievalLabelProvenance
    cases: list[RetrievalR3ReplayCase] = Field(min_length=1, max_length=50)
    annotators: list[str] = Field(default_factory=list, max_length=10)
    thresholds: ReplayThresholds
