"""Private learning evidence and immutable replay manifests (v1.6).

These records never confer knowledge publication, execution permission or global mastery.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, Row


class LearningProfile(Row, Base):
    __tablename__ = "learning_profiles"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), unique=True)
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active_plan_id: Mapped[str | None] = mapped_column(ForeignKey("learning_plans.id"))
    active_module_id: Mapped[str | None] = mapped_column(ForeignKey("dependency_objects.id"))


class PlanProposalManifest(Row, Base):
    # Presentation versions live inside the immutable payload. Old 1.6 manifests
    # retain their original bytes/hash; readers project legacy gaps conservatively.
    # The 1.6.1 payload also binds module hashes and safe display projections, so
    # this additive contract does not require a database migration or a rewrite.
    __tablename__ = "plan_proposal_manifests"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    plan_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id"), unique=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    selection_manifest_id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class LearningActivity(Base):
    __tablename__ = "learning_activities"
    id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"), primary_key=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    module_id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"))
    kind: Mapped[str] = mapped_column(String(32))
    specification_hash: Mapped[str] = mapped_column(String(64))
    specification: Mapped[dict[str, Any]] = mapped_column(JSON)


class LearningAttempt(Row, Base):
    __tablename__ = "learning_attempts"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    activity_id: Mapped[str] = mapped_column(ForeignKey("learning_activities.id"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), unique=True)
    specification_hash: Mapped[str] = mapped_column(String(64))
    assistance_mode: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(24), default="started")
    response: Mapped[str] = mapped_column(Text, default="")
    transfer_notes: Mapped[str] = mapped_column(Text, default="")
    external_help: Mapped[str] = mapped_column(Text, default="unknown")
    submission_id: Mapped[str | None] = mapped_column(ForeignKey("submissions.id"))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    submission_hash: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(150))
    __table_args__ = (UniqueConstraint("principal_id", "idempotency_key"),)


class LearningObservationRecord(Row, Base):
    __tablename__ = "learning_observation_records"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("learning_attempts.id"))
    evaluator_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    evaluator_kind: Mapped[str] = mapped_column(String(24))
    hypothesis_status: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="active")
    rubric_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    observed_behavior: Mapped[str] = mapped_column(Text)
    assistance_mode: Mapped[str] = mapped_column(String(32))
    knowledge_version_refs: Mapped[list[str]] = mapped_column(JSON)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON)
    assessment: Mapped[str] = mapped_column(String(60))
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("learning_observation_records.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)


class LearningAdjustment(Row, Base):
    __tablename__ = "learning_adjustments"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    plan_id: Mapped[str] = mapped_column(ForeignKey("learning_plans.id"))
    source_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id"))
    proposed_version_id: Mapped[str] = mapped_column(ForeignKey("plan_versions.id"), unique=True)
    observation_ids: Mapped[list[str]] = mapped_column(JSON)
    strategy: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)


class PolicyReplayManifest(Row, Base):
    __tablename__ = "policy_replay_manifests"
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("evolution_candidates.id"))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("evaluation_datasets.id"))
    evaluation_id: Mapped[str | None] = mapped_column(
        ForeignKey("evolution_evaluations.id"), unique=True
    )
    frozen_hash: Mapped[str] = mapped_column(String(64))
    frozen_input: Mapped[dict[str, Any]] = mapped_column(JSON)
    trace_hash: Mapped[str | None] = mapped_column(String(64))
    trace: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class EvaluationDatasetAttestation(Row, Base):
    __tablename__ = "evaluation_dataset_attestations"
    dataset_id: Mapped[str] = mapped_column(ForeignKey("evaluation_datasets.id"))
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    frozen_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(24))
    notes: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("dataset_id", "principal_id"),)
