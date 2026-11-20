"""Authoritative relational state. JSON columns contain frozen, typed artifact payloads.

No cache or vector index is allowed to become a second authorization/publication source.
"""

from __future__ import annotations

from datetime import datetime
from importlib import import_module
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, UserDefinedType

from .util import now, uid


class Base(DeclarativeBase):
    pass


class Row:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Principal(Row, Base):
    __tablename__ = "principals"
    name: Mapped[str] = mapped_column(String(120), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    auth_revision: Mapped[int] = mapped_column(Integer, default=1)


class Namespace(Row, Base):
    __tablename__ = "knowledge_namespaces"
    kind: Mapped[str] = mapped_column(String(24))
    owner_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    name: Mapped[str] = mapped_column(String(120))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (CheckConstraint("kind IN ('private','shared_public','workspace')"),)


class AccessGrant(Row, Base):
    __tablename__ = "access_grants"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    role: Mapped[str] = mapped_column(String(12), default="reader")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("namespace_id", "principal_id"),)


class ObjectRow(Row, Base):
    __tablename__ = "dependency_objects"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(24), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("id", "kind", "namespace_id"),
        CheckConstraint("state IN ('active','suspended','revoked')"),
    )


class ArtifactDependency(Row, Base):
    __tablename__ = "artifact_dependencies"
    edge_namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    dependent_id: Mapped[str] = mapped_column(String(36))
    dependent_type: Mapped[str] = mapped_column(String(40))
    dependent_namespace_id: Mapped[str] = mapped_column(String(36))
    source_id: Mapped[str] = mapped_column(String(36))
    source_type: Mapped[str] = mapped_column(String(40))
    source_namespace_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(40))
    enforcement: Mapped[str] = mapped_column(String(20))
    authorization_ref: Mapped[str] = mapped_column(String(120))
    authorization_revision: Mapped[int] = mapped_column(Integer)
    policy_version: Mapped[str] = mapped_column(String(40), default="dependency-1.5")
    __table_args__ = (
        ForeignKeyConstraint(
            ["dependent_id", "dependent_type", "dependent_namespace_id"],
            ["dependency_objects.id", "dependency_objects.kind", "dependency_objects.namespace_id"],
        ),
        ForeignKeyConstraint(
            ["source_id", "source_type", "source_namespace_id"],
            ["dependency_objects.id", "dependency_objects.kind", "dependency_objects.namespace_id"],
        ),
        CheckConstraint("edge_namespace_id = dependent_namespace_id"),
        CheckConstraint("dependent_id != source_id"),
        CheckConstraint("enforcement IN ('blocking','impact_only','provenance_only')"),
        UniqueConstraint("dependent_id", "source_id", "kind", "enforcement", "policy_version"),
        Index("ix_dependencies_reverse", "source_id", "source_type", "source_namespace_id"),
        Index("ix_dependencies_forward", "dependent_id"),
    )


class SourceSet(Base):
    __tablename__ = "source_sets"
    id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"), primary_key=True)
    origin_kind: Mapped[str] = mapped_column(String(20))
    repository_key: Mapped[str] = mapped_column(String(500))
    commit_sha: Mapped[str | None] = mapped_column(String(40))
    scope_hash: Mapped[str] = mapped_column(String(64))
    license_id: Mapped[str] = mapped_column(String(100))
    files: Mapped[dict[str, str]] = mapped_column(JSON)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SourceRead(Row, Base):
    __tablename__ = "source_reads"
    source_id: Mapped[str] = mapped_column(ForeignKey("source_sets.id"))
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))
    session_key: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(40))
    path: Mapped[str] = mapped_column(String(1000))
    first_line: Mapped[int] = mapped_column(Integer)
    last_line: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    file_hash: Mapped[str] = mapped_column(String(64))
    quote_hash: Mapped[str] = mapped_column(String(64))


class Evidence(Base):
    __tablename__ = "evidence_refs"
    id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source_sets.id"))
    read_id: Mapped[str] = mapped_column(ForeignKey("source_reads.id"))
    path: Mapped[str] = mapped_column(String(1000))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    exact_quote: Mapped[str] = mapped_column(Text)
    file_hash: Mapped[str] = mapped_column(String(64))
    quote_hash: Mapped[str] = mapped_column(String(64))


class CodeSymbol(Row, Base):
    __tablename__ = "code_symbols"
    source_id: Mapped[str] = mapped_column(ForeignKey("source_sets.id"), index=True)
    path: Mapped[str] = mapped_column(String(1000))
    name: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(40))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    parser: Mapped[str] = mapped_column(String(120))
    __table_args__ = (Index("ix_symbol_exact", "source_id", "name"),)


class Topic(Row, Base):
    __tablename__ = "technology_topics"
    key: Mapped[str] = mapped_column(String(120), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    aliases: Mapped[list[str]] = mapped_column(JSON)
    facet_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)


class KnowledgeUnit(Row, Base):
    __tablename__ = "knowledge_units"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    origin_kind: Mapped[str] = mapped_column(String(20))
    origin_id: Mapped[str] = mapped_column(String(500))
    unit_type: Mapped[str] = mapped_column(String(30))
    subject_key: Mapped[str] = mapped_column(String(200))
    applicability_key: Mapped[str] = mapped_column(String(64))
    next_revision: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        UniqueConstraint(
            "namespace_id",
            "origin_kind",
            "origin_id",
            "unit_type",
            "subject_key",
            "applicability_key",
        ),
    )


class UnitVersion(Base):
    __tablename__ = "knowledge_unit_versions"
    id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"), primary_key=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("knowledge_units.id"))
    revision_no: Mapped[int] = mapped_column(Integer)
    source_id: Mapped[str] = mapped_column(ForeignKey("source_sets.id"))
    title: Mapped[str] = mapped_column(String(200))
    content_hash: Mapped[str] = mapped_column(String(64))
    render_hash: Mapped[str] = mapped_column(String(64))
    analysis_run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    render_ast: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    dependency_manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    limitations: Mapped[list[str]] = mapped_column(JSON, default=list)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("unit_id", "revision_no"),)


class Claim(Base):
    __tablename__ = "claims"
    id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"), primary_key=True)
    unit_version_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_unit_versions.id"), index=True
    )
    statement: Mapped[str] = mapped_column(Text)
    conditions: Mapped[list[str]] = mapped_column(JSON)
    criticality: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(30))


class FactAtom(Base):
    __tablename__ = "fact_atoms"
    id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"), primary_key=True)
    unit_version_id: Mapped[str] = mapped_column(ForeignKey("knowledge_unit_versions.id"))
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id"))
    approved_text: Mapped[str] = mapped_column(Text)
    conditions: Mapped[list[str]] = mapped_column(JSON)


class TopicLink(Base):
    __tablename__ = "knowledge_unit_topics"
    id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"), primary_key=True)
    unit_version_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_unit_versions.id"), index=True
    )
    topic_id: Mapped[str] = mapped_column(ForeignKey("technology_topics.id"), index=True)
    relation: Mapped[str] = mapped_column(String(24))
    __table_args__ = (UniqueConstraint("unit_version_id", "topic_id", "relation"),)


class FacetValue(Base):
    __tablename__ = "facet_values"
    id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"), primary_key=True)
    unit_version_id: Mapped[str] = mapped_column(ForeignKey("knowledge_unit_versions.id"))
    topic_id: Mapped[str] = mapped_column(ForeignKey("technology_topics.id"))
    schema_version: Mapped[int] = mapped_column(Integer)
    facet_key: Mapped[str] = mapped_column(String(100))
    value_status: Mapped[str] = mapped_column(String(30))
    value: Mapped[Any] = mapped_column(JSON)
    claim_ids: Mapped[list[str]] = mapped_column(JSON)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("unit_version_id", "topic_id", "schema_version", "facet_key"),
    )


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"), primary_key=True)
    unit_version_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_unit_versions.id"), index=True
    )
    reviewer_run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    independent_bundle_hash: Mapped[str] = mapped_column(String(64))
    reviewed_content_hash: Mapped[str] = mapped_column(String(64))
    reviewed_render_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(24))
    report: Mapped[dict[str, Any]] = mapped_column(JSON)


class Publication(Base):
    __tablename__ = "publications"
    id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"), primary_key=True)
    unit_version_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_unit_versions.id"), unique=True
    )
    review_id: Mapped[str] = mapped_column(ForeignKey("review_decisions.id"))
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"), index=True)


class SelectionHead(Base):
    __tablename__ = "knowledge_selection_heads"
    id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"), primary_key=True)
    selection_namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    unit_id: Mapped[str] = mapped_column(ForeignKey("knowledge_units.id"))
    selection_context: Mapped[str] = mapped_column(String(80), default="default")
    scope_key: Mapped[str] = mapped_column(String(64))  # repository: source_id; concept: "concept"
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        UniqueConstraint("selection_namespace_id", "unit_id", "selection_context", "scope_key"),
    )


class ProjectSelection(Row, Base):
    __tablename__ = "project_snapshot_selections"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    repository_key: Mapped[str] = mapped_column(String(500))
    source_id: Mapped[str] = mapped_column(ForeignKey("source_sets.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("namespace_id", "repository_key"),)


class FundingAgreement(Row, Base):
    __tablename__ = "funding_agreements"
    payer_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    kind: Mapped[str] = mapped_column(String(16), default="private")
    status: Mapped[str] = mapped_column(String(24), default="active")
    call_cap: Mapped[int] = mapped_column(Integer)
    calls_consumed: Mapped[int] = mapped_column(Integer, default=0)
    calls_held: Mapped[int] = mapped_column(Integer, default=0)
    allowed_roles: Mapped[list[str]] = mapped_column(JSON)
    allowed_source_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    allowed_data_classes: Mapped[list[str]] = mapped_column(JSON)
    credential_profile: Mapped[str] = mapped_column(String(80), default="default")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "calls_consumed >= 0 AND calls_held >= 0 AND calls_consumed + calls_held <= call_cap"
        ),
    )


class AgentRun(Row, Base):
    __tablename__ = "agent_runs"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    funding_id: Mapped[str] = mapped_column(ForeignKey("funding_agreements.id"))
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    workflow: Mapped[str] = mapped_column(String(40))
    workflow_engine: Mapped[str] = mapped_column(
        String(16), default="legacy", server_default="legacy"
    )
    graph_version: Mapped[str | None] = mapped_column(String(40))
    state_schema_version: Mapped[int | None] = mapped_column(Integer)
    contract_version: Mapped[str] = mapped_column(String(40), default="careerforge-1.5")
    input_hash: Mapped[str] = mapped_column(String(64))
    input_manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    worker_id: Mapped[str | None] = mapped_column(String(120))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    call_cap: Mapped[int] = mapped_column(Integer)
    calls_consumed: Mapped[int] = mapped_column(Integer, default=0)
    held_calls: Mapped[int] = mapped_column(Integer, default=0)
    budget_settled: Mapped[bool] = mapped_column(Boolean, default=False)
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        CheckConstraint(
            "calls_consumed >= 0 AND held_calls >= 0 AND calls_consumed + held_calls <= call_cap"
        ),
    )


class RunAllocation(Row, Base):
    __tablename__ = "run_allocations"
    parent_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    child_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    task_key: Mapped[str] = mapped_column(String(100))
    allocated: Mapped[int] = mapped_column(Integer)
    settled: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("parent_id", "task_key"),)


class StageArtifact(Row, Base):
    __tablename__ = "stage_artifacts"
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    stage_key: Mapped[str] = mapped_column(String(200))
    input_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("run_id", "stage_key"),)


class RunEvent(Base):
    __tablename__ = "run_events"
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ModelReceipt(Row, Base):
    __tablename__ = "model_call_receipts"
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    stage_key: Mapped[str] = mapped_column(String(200))
    payer_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    epoch: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(40))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="attempted")
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    profile_version: Mapped[str] = mapped_column(String(120))
    __table_args__ = (UniqueConstraint("run_id", "stage_key"),)


class Outbox(Row, Base):
    __tablename__ = "outbox_messages"
    kind: Mapped[str] = mapped_column(String(30))
    aggregate_id: Mapped[str] = mapped_column(String(36))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Idempotency(Row, Base):
    __tablename__ = "idempotency_records"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    operation: Mapped[str] = mapped_column(String(150))
    key: Mapped[str] = mapped_column(String(150))
    request_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("principal_id", "operation", "key"),)


class ResearchNode(Row, Base):
    __tablename__ = "research_nodes"
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("research_nodes.id"))
    depth: Mapped[int] = mapped_column(Integer)
    question: Mapped[str] = mapped_column(Text)
    node_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("run_id", "node_key"),)


class SearchObservation(Row, Base):
    __tablename__ = "search_observations"
    node_id: Mapped[str] = mapped_column(ForeignKey("research_nodes.id"))
    query_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("node_id", "query_hash"),)


class Conversation(Row, Base):
    __tablename__ = "conversations"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    focus: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Turn(Row, Base):
    __tablename__ = "conversation_turns"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    question: Mapped[str] = mapped_column(Text)
    focus_revision: Mapped[int] = mapped_column(Integer)
    focus: Mapped[dict[str, Any]] = mapped_column(JSON)
    policy_versions: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))
    manifest_id: Mapped[str | None] = mapped_column(ForeignKey("dependency_objects.id"))
    idempotency_key: Mapped[str] = mapped_column(String(150))
    __table_args__ = (UniqueConstraint("conversation_id", "idempotency_key"),)


class AnswerBlock(Base):
    __tablename__ = "answer_blocks"
    id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"), primary_key=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("conversation_turns.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("turn_id", "ordinal"),)


class StreamEvent(Row, Base):
    __tablename__ = "stream_events"
    turn_id: Mapped[str] = mapped_column(ForeignKey("conversation_turns.id"))
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("turn_id", "seq"),)


class Feedback(Row, Base):
    __tablename__ = "feedback_events"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    turn_id: Mapped[str] = mapped_column(ForeignKey("conversation_turns.id"))
    signal: Mapped[str] = mapped_column(String(40))
    origin: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(Text)
    consent_revision: Mapped[int] = mapped_column(Integer)


class Experience(Row, Base):
    __tablename__ = "experience_episodes"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    feedback_id: Mapped[str] = mapped_column(ForeignKey("feedback_events.id"), unique=True)
    diagnosis: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class PolicyVersion(Row, Base):
    __tablename__ = "policy_versions"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    target: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    candidate_id: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = (UniqueConstraint("namespace_id", "target", "version"),)


class PolicyHead(Row, Base):
    __tablename__ = "policy_heads"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    target: Mapped[str] = mapped_column(String(40))
    policy_id: Mapped[str] = mapped_column(ForeignKey("policy_versions.id"))
    previous_policy_id: Mapped[str | None] = mapped_column(ForeignKey("policy_versions.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("namespace_id", "target"),)


class EvolutionCandidate(Row, Base):
    __tablename__ = "evolution_candidates"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    target: Mapped[str] = mapped_column(String(40))
    base_policy_id: Mapped[str] = mapped_column(ForeignKey("policy_versions.id"))
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    experience_ids: Mapped[list[str]] = mapped_column(JSON)
    training_group_ids: Mapped[list[str]] = mapped_column(JSON)
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("principals.id"))


class EvalDataset(Row, Base):
    __tablename__ = "evaluation_datasets"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(40))
    provenance: Mapped[str] = mapped_column(String(40))
    frozen_hash: Mapped[str] = mapped_column(String(64))
    cases: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    split: Mapped[str] = mapped_column(String(20))
    threshold_policy: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("namespace_id", "name", "version"),)


class EvolutionEvaluation(Row, Base):
    __tablename__ = "evolution_evaluations"
    candidate_id: Mapped[str] = mapped_column(ForeignKey("evolution_candidates.id"))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("evaluation_datasets.id"))
    candidate_hash: Mapped[str] = mapped_column(String(64))
    base_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(20))
    report: Mapped[dict[str, Any]] = mapped_column(JSON)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))


class Dispute(Row, Base):
    __tablename__ = "disputes"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    target_id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"))
    status: Mapped[str] = mapped_column(String(24), default="open")
    reason: Mapped[str] = mapped_column(Text)
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Impact(Row, Base):
    __tablename__ = "invalidation_impacts"
    source_id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"))
    consumer_id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"))
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    generation: Mapped[int] = mapped_column(Integer)
    enforcement: Mapped[str] = mapped_column(String(20))
    __table_args__ = (UniqueConstraint("source_id", "consumer_id", "generation"),)


class Workspace(Row, Base):
    __tablename__ = "workspaces"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    files: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    exercise_id: Mapped[str | None] = mapped_column(ForeignKey("dependency_objects.id"))
    assistance_mode: Mapped[str] = mapped_column(String(24), default="assisted")
    sealed: Mapped[bool] = mapped_column(Boolean, default=False)


class PatchProposal(Row, Base):
    __tablename__ = "patch_proposals"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    expected_revision: Mapped[int] = mapped_column(Integer)
    expected_manifest: Mapped[str] = mapped_column(String(64))
    edits: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    applied_revision: Mapped[int | None] = mapped_column(Integer)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))


class ExecutionSnapshot(Row, Base):
    __tablename__ = "execution_snapshots"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    workspace_revision: Mapped[int] = mapped_column(Integer)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    files: Mapped[dict[str, str]] = mapped_column(JSON)
    image_digest: Mapped[str] = mapped_column(String(250))
    environment_version: Mapped[str] = mapped_column(String(100))


class CommandAuthorization(Row, Base):
    __tablename__ = "command_authorizations"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    execution_snapshot_id: Mapped[str] = mapped_column(ForeignKey("execution_snapshots.id"))
    argv: Mapped[list[str]] = mapped_column(JSON)
    argv_hash: Mapped[str] = mapped_column(String(64))
    nonce_hash: Mapped[str] = mapped_column(String(64))
    # Null means a legacy, unbound approval: it must never be executed.
    authorization_digest: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(24), default="proposed")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class PGVector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "vector"


class Vector(TypeDecorator):
    """Postgres vector; JSON only for the explicitly selected offline test backend."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        return dialect.type_descriptor(PGVector() if dialect.name == "postgresql" else JSON())

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        return (
            "[" + ",".join(str(float(x)) for x in value) + "]"
            if dialect.name == "postgresql"
            else value
        )

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if isinstance(value, str):
            import json

            return json.loads(value)
        return value


class KnowledgeEmbedding(Base):
    __tablename__ = "knowledge_embeddings"
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), primary_key=True)
    model_version: Mapped[str] = mapped_column(String(120), primary_key=True)
    dimensions: Mapped[int] = mapped_column(Integer)
    vector: Mapped[list[float]] = mapped_column(Vector())
    search_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))


class ExternalRequest(Row, Base):
    __tablename__ = "external_requests"
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    operation: Mapped[str] = mapped_column(String(40))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="attempted")
    response_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_search_documents"
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), primary_key=True)
    search_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))


class CareerGoal(Row, Base):
    __tablename__ = "career_goals"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    title: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    active_version_id: Mapped[str | None] = mapped_column(String(36))


class GoalVersion(Row, Base):
    __tablename__ = "career_goal_versions"
    goal_id: Mapped[str] = mapped_column(ForeignKey("career_goals.id"))
    version: Mapped[int] = mapped_column(Integer)
    source_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    requirements: Mapped[dict[str, Any]] = mapped_column(JSON)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    origin_contract: Mapped[str] = mapped_column(
        String(40), default="run-goal-v1", server_default="run-goal-v1"
    )
    parent_goal_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("career_goal_versions.id", name="fk_goal_version_parent"), index=True
    )
    goal_addendum_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_goal_addenda.id", name="fk_goal_version_addendum", use_alter=True),
    )
    version_hash: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("goal_id", "version"),
        UniqueConstraint("goal_addendum_id", name="uq_goal_version_addendum"),
        CheckConstraint(
            "(origin_contract = 'run-goal-v1' AND run_id IS NOT NULL "
            "AND parent_goal_version_id IS NULL AND goal_addendum_id IS NULL "
            "AND version_hash IS NULL) OR "
            "(origin_contract = 'command-goal-v1' AND run_id IS NULL "
            "AND parent_goal_version_id IS NOT NULL AND goal_addendum_id IS NOT NULL "
            "AND version_hash IS NOT NULL AND version >= 2)",
            name="ck_goal_version_origin",
        ),
    )


class LearningPlan(Row, Base):
    __tablename__ = "learning_plans"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    active_version_id: Mapped[str | None] = mapped_column(String(36))


class PlanVersion(Row, Base):
    __tablename__ = "plan_versions"
    plan_id: Mapped[str] = mapped_column(ForeignKey("learning_plans.id"))
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    learning_intent: Mapped[str] = mapped_column(String(40))
    module_ids: Mapped[list[str]] = mapped_column(JSON)
    source_version_id: Mapped[str | None] = mapped_column(ForeignKey("plan_versions.id"))
    change_reason: Mapped[str] = mapped_column(Text, default="initial")
    __table_args__ = (UniqueConstraint("plan_id", "version"),)


class TeachingObservation(Row, Base):
    __tablename__ = "teaching_observations"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    module_id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"))
    response: Mapped[str] = mapped_column(Text)
    rubric: Mapped[dict[str, Any]] = mapped_column(JSON)
    help_mode: Mapped[str] = mapped_column(String(30))
    evaluator: Mapped[str] = mapped_column(String(30))
    competence_verified: Mapped[bool] = mapped_column(Boolean, default=False)


class Submission(Row, Base):
    __tablename__ = "submissions"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    workspace_revision: Mapped[int] = mapped_column(Integer)
    code_hash: Mapped[str] = mapped_column(String(64))
    files: Mapped[dict[str, str]] = mapped_column(JSON)
    explanation: Mapped[str] = mapped_column(Text)
    transfer_notes: Mapped[str] = mapped_column(Text)
    help_conditions: Mapped[dict[str, Any]] = mapped_column(JSON)
    execution_receipt_ids: Mapped[list[str]] = mapped_column(JSON)
    rubric: Mapped[dict[str, Any]] = mapped_column(JSON)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str] = mapped_column(String(150))
    __table_args__ = (UniqueConstraint("principal_id", "workspace_id", "idempotency_key"),)


class PolicyRollout(Row, Base):
    __tablename__ = "policy_rollouts"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"), index=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("evolution_candidates.id"))
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("evolution_evaluations.id"))
    target: Mapped[str] = mapped_column(String(40))
    base_policy_id: Mapped[str] = mapped_column(ForeignKey("policy_versions.id"))
    trial_policy_id: Mapped[str] = mapped_column(ForeignKey("policy_versions.id"))
    head_revision: Mapped[int] = mapped_column(Integer)
    traffic_percent: Mapped[int] = mapped_column(Integer)
    minimum_per_arm: Mapped[int] = mapped_column(Integer)
    maximum_turns: Mapped[int] = mapped_column(Integer)
    max_guard_regression: Mapped[float] = mapped_column(Float)
    max_latency_ratio: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24), default="active")
    reason: Mapped[str | None] = mapped_column(String(120))
    approved_by: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    __table_args__ = (CheckConstraint("traffic_percent >= 1 AND traffic_percent <= 50"),)


class PolicyObservation(Row, Base):
    __tablename__ = "policy_observations"
    rollout_id: Mapped[str] = mapped_column(ForeignKey("policy_rollouts.id"), index=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("conversation_turns.id"))
    arm: Mapped[str] = mapped_column(String(12))
    guard_blocks: Mapped[int] = mapped_column(Integer)
    evidence_gaps: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24))
    __table_args__ = (UniqueConstraint("rollout_id", "turn_id"),)


class KnowledgeGap(Row, Base):
    __tablename__ = "knowledge_gaps"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"), index=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_turns.id"), unique=True)
    topic_keys: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    proposed_route: Mapped[str] = mapped_column(String(24), default="analysis")
    status: Mapped[str] = mapped_column(String(24), default="open")
    build_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))


class DisputeResolution(Row, Base):
    __tablename__ = "dispute_resolutions"
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id"), unique=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    outcome: Mapped[str] = mapped_column(String(24))
    fresh_review_id: Mapped[str | None] = mapped_column(ForeignKey("review_decisions.id"))
    replacement_publication_id: Mapped[str | None] = mapped_column(ForeignKey("publications.id"))
    reason: Mapped[str] = mapped_column(Text)


def _register_extension_models() -> None:
    # Import only the fixed application modules after Base and original tables exist.
    # This keeps create_all, Alembic metadata, and individual service tests consistent.
    for module in (
        "models_governance",
        "models_runtime_ext",
        "models_retrieval_ext",
        "models_learning_ext",
        "models_privacy_ext",
        "models_compat",
        "models_dialogue",
    ):
        import_module(f"careerforge.{module}")


_register_extension_models()
