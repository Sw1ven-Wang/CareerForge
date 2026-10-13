from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Timestamped, Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)


class UserSession(Timestamped, Base):
    __tablename__ = "user_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginAttempt(Timestamped, Base):
    __tablename__ = "login_attempts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    identity_hash: Mapped[str] = mapped_column(String(64), index=True)


class CareerGoal(Timestamped, Base):
    __tablename__ = "career_goals"
    __table_args__ = (UniqueConstraint("id", "owner_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    revision: Mapped[int] = mapped_column(default=1)


class CareerGoalVersion(Timestamped, Base):
    __tablename__ = "career_goal_versions"
    __table_args__ = (
        ForeignKeyConstraint(["goal_id", "owner_id"], ["career_goals.id", "career_goals.owner_id"]),
        UniqueConstraint("goal_id", "version_no"),
        UniqueConstraint("id", "owner_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    goal_id: Mapped[UUID]
    owner_id: Mapped[UUID]
    version_no: Mapped[int]
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    change_reason: Mapped[str | None] = mapped_column(Text)


class Source(Timestamped, Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("id", "owner_id"),
        ForeignKeyConstraint(
            ["library_category_id", "owner_id"],
            ["source_library_categories.id", "source_library_categories.owner_id"],
            name="fk_sources_library_category_owner",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    identity_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    import_mode: Mapped[str] = mapped_column(String(20))
    source_type: Mapped[str] = mapped_column(String(40))
    title: Mapped[str | None] = mapped_column(Text)
    library_category_id: Mapped[UUID | None]
    revision: Mapped[int] = mapped_column(default=1)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceSnapshot(Timestamped, Base):
    __tablename__ = "source_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(["source_id", "owner_id"], ["sources.id", "sources.owner_id"]),
        UniqueConstraint("source_id", "version_no"),
        UniqueConstraint("id", "owner_id"),
        UniqueConstraint("id", "source_id", "owner_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_id: Mapped[UUID]
    owner_id: Mapped[UUID]
    version_no: Mapped[int]
    attachment_id: Mapped[UUID | None] = mapped_column(ForeignKey("attachments.id"))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    metadata_hash: Mapped[str] = mapped_column(String(64))
    metadata_values: Mapped[dict[str, Any]] = mapped_column(JSONB)
    change_reason: Mapped[str | None] = mapped_column(Text)


class Citation(Timestamped, Base):
    __tablename__ = "citations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "source_id", "owner_id"],
            ["source_snapshots.id", "source_snapshots.source_id", "source_snapshots.owner_id"],
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_id: Mapped[UUID]
    snapshot_id: Mapped[UUID]
    owner_id: Mapped[UUID]
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    text_hash: Mapped[str] = mapped_column(String(64))


class IdempotencyRecord(Timestamped, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("owner_id", "operation", "key"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    operation: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Deletion(Timestamped, Base):
    __tablename__ = "deletions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    source_id: Mapped[UUID | None] = mapped_column(ForeignKey("sources.id"))
    attachment_id: Mapped[UUID | None] = mapped_column(ForeignKey("attachments.id"))
    status: Mapped[str] = mapped_column(String(20), default="revoked")


class AgentRun(Timestamped, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("id", "owner_id"),
        ForeignKeyConstraint(
            ["goal_version_id", "owner_id"],
            ["career_goal_versions.id", "career_goal_versions.owner_id"],
        ),
        CheckConstraint(
            "call_budget >= 0 AND calls_reserved >= 0 AND calls_reserved <= call_budget",
            name="ck_run_budget",
        ),
        CheckConstraint(
            "status IN ('queued','running','waiting_user','succeeded',"
            "'partially_succeeded','failed','budget_stopped','cancelled')",
            name="ck_run_status",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    goal_version_id: Mapped[UUID | None]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.clock_timestamp()
    )
    workflow_type: Mapped[str] = mapped_column(String(40))
    workflow_version: Mapped[str] = mapped_column(String(80))
    input_manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    input_manifest_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    version: Mapped[int] = mapped_column(default=1)
    epoch: Mapped[int] = mapped_column(default=0)
    worker_id: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_seq: Mapped[int] = mapped_column(default=0)
    min_event_seq: Mapped[int] = mapped_column(default=1)
    call_budget: Mapped[int] = mapped_column(default=3)
    calls_reserved: Mapped[int] = mapped_column(default=0)


class RunAttempt(Timestamped, Base):
    __tablename__ = "run_attempts"
    __table_args__ = (UniqueConstraint("run_id", "epoch"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"))
    epoch: Mapped[int]
    worker_id: Mapped[str] = mapped_column(String(100))


class RunEvent(Timestamped, Base):
    __tablename__ = "run_events"
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(primary_key=True)
    schema_version: Mapped[int] = mapped_column(default=1)
    type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class StageArtifact(Timestamped, Base):
    __tablename__ = "stage_artifacts"
    __table_args__ = (
        UniqueConstraint("run_id", "stage_key", "input_fingerprint", "stage_version"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"))
    stage_key: Mapped[str] = mapped_column(String(100))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    stage_version: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class WaitRequest(Timestamped, Base):
    __tablename__ = "wait_requests"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "stage_key", "input_fingerprint", "question_version", "wait_sequence"
        ),
        UniqueConstraint("id", "run_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"))
    stage_key: Mapped[str] = mapped_column(String(100))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    question_version: Mapped[str] = mapped_column(String(80))
    wait_sequence: Mapped[int] = mapped_column(default=1)
    question: Mapped[dict[str, Any]] = mapped_column(JSONB)
    response_schema: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ResumeDecision(Timestamped, Base):
    __tablename__ = "resume_decisions"
    wait_request_id: Mapped[UUID] = mapped_column(ForeignKey("wait_requests.id"), primary_key=True)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    response_hash: Mapped[str] = mapped_column(String(64))


class OutboxMessage(Timestamped, Base):
    __tablename__ = "outbox_messages"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelCallReceipt(Timestamped, Base):
    __tablename__ = "model_call_receipts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"))
    epoch: Mapped[int]
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="reserved")
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Attachment(Timestamped, Base):
    __tablename__ = "attachments"
    __table_args__ = (
        ForeignKeyConstraint(["source_id", "owner_id"], ["sources.id", "sources.owner_id"]),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[UUID | None]
    evidence_id: Mapped[UUID | None] = mapped_column(ForeignKey("personal_evidence.id"))
    name: Mapped[str] = mapped_column(String(200))
    mime: Mapped[str] = mapped_column(String(100))
    submission_id: Mapped[UUID | None] = mapped_column(ForeignKey("submissions.id"))
    purpose: Mapped[str] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(String(64))
    body: Mapped[bytes | None] = mapped_column(LargeBinary)
    size: Mapped[int]
    revision: Mapped[int] = mapped_column(default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobPostingVersion(Timestamped, Base):
    __tablename__ = "job_posting_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "source_id", "owner_id"],
            ["source_snapshots.id", "source_snapshots.source_id", "source_snapshots.owner_id"],
        ),
        UniqueConstraint("snapshot_id"),
        UniqueConstraint("id", "owner_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    source_id: Mapped[UUID]
    snapshot_id: Mapped[UUID]


class RequirementCollection(Timestamped, Base):
    __tablename__ = "requirement_collections"
    __table_args__ = (UniqueConstraint("posting_version_id", "extractor_version"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    posting_version_id: Mapped[UUID] = mapped_column(ForeignKey("job_posting_versions.id"))
    extractor_version: Mapped[str] = mapped_column(String(80))
    revision: Mapped[int] = mapped_column(default=1)


class Requirement(Timestamped, Base):
    __tablename__ = "requirements"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    collection_id: Mapped[UUID] = mapped_column(ForeignKey("requirement_collections.id"))
    collection_version: Mapped[int]
    supersedes_id: Mapped[UUID | None] = mapped_column(ForeignKey("requirements.id"))
    citation_id: Mapped[UUID] = mapped_column(ForeignKey("citations.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class DuplicateCandidate(Timestamped, Base):
    __tablename__ = "duplicate_candidates"
    __table_args__ = (UniqueConstraint("goal_version_id", "left_id", "right_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    goal_version_id: Mapped[UUID] = mapped_column(ForeignKey("career_goal_versions.id"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"))
    left_id: Mapped[UUID] = mapped_column(ForeignKey("job_posting_versions.id"))
    right_id: Mapped[UUID] = mapped_column(ForeignKey("job_posting_versions.id"))
    revision: Mapped[int] = mapped_column(default=1)
    reason: Mapped[str] = mapped_column(String(100))


class DedupDecision(Timestamped, Base):
    __tablename__ = "dedup_decisions"
    __table_args__ = (UniqueConstraint("candidate_id", "version_no"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("duplicate_candidates.id"))
    version_no: Mapped[int]
    decision: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)


class MarketReport(Timestamped, Base):
    __tablename__ = "market_reports"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    goal_version_id: Mapped[UUID] = mapped_column(ForeignKey("career_goal_versions.id"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class PersonalEvidence(Timestamped, Base):
    __tablename__ = "personal_evidence"
    __table_args__ = (UniqueConstraint("id", "owner_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    revision: Mapped[int] = mapped_column(default=1)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PersonalEvidenceVersion(Timestamped, Base):
    __tablename__ = "personal_evidence_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["evidence_id", "owner_id"], ["personal_evidence.id", "personal_evidence.owner_id"]
        ),
        UniqueConstraint("evidence_id", "version_no"),
        UniqueConstraint("id", "owner_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    evidence_id: Mapped[UUID]
    owner_id: Mapped[UUID]
    version_no: Mapped[int]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))


class LearningObjective(Timestamped, Base):
    __tablename__ = "learning_objectives"
    __table_args__ = (UniqueConstraint("id", "owner_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    goal_id: Mapped[UUID] = mapped_column(ForeignKey("career_goals.id"))
    revision: Mapped[int] = mapped_column(default=1)


class LearningObjectiveVersion(Timestamped, Base):
    __tablename__ = "learning_objective_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["objective_id", "owner_id"], ["learning_objectives.id", "learning_objectives.owner_id"]
        ),
        ForeignKeyConstraint(
            ["goal_version_id", "owner_id"],
            ["career_goal_versions.id", "career_goal_versions.owner_id"],
        ),
        UniqueConstraint("objective_id", "version_no"),
        UniqueConstraint("id", "owner_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    objective_id: Mapped[UUID]
    owner_id: Mapped[UUID]
    goal_version_id: Mapped[UUID]
    version_no: Mapped[int]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))


class AcceptanceCriterionVersion(Timestamped, Base):
    __tablename__ = "acceptance_criterion_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["objective_version_id", "owner_id"],
            ["learning_objective_versions.id", "learning_objective_versions.owner_id"],
        ),
        UniqueConstraint("id", "owner_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    objective_version_id: Mapped[UUID]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class AlignmentReport(Timestamped, Base):
    __tablename__ = "alignment_reports"
    __table_args__ = (UniqueConstraint("id", "owner_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    goal_version_id: Mapped[UUID] = mapped_column(ForeignKey("career_goal_versions.id"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class LearningPlan(Timestamped, Base):
    __tablename__ = "learning_plans"
    __table_args__ = (UniqueConstraint("id", "owner_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    goal_id: Mapped[UUID] = mapped_column(ForeignKey("career_goals.id"), unique=True)
    revision: Mapped[int] = mapped_column(default=1)
    active_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("learning_plan_versions.id", use_alter=True, name="fk_plan_active_version")
    )


class LearningPlanVersion(Timestamped, Base):
    __tablename__ = "learning_plan_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "owner_id"], ["learning_plans.id", "learning_plans.owner_id"]
        ),
        ForeignKeyConstraint(
            ["goal_version_id", "owner_id"],
            ["career_goal_versions.id", "career_goal_versions.owner_id"],
        ),
        UniqueConstraint("plan_id", "version_no"),
        UniqueConstraint("id", "owner_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    plan_id: Mapped[UUID]
    goal_version_id: Mapped[UUID]
    version_no: Mapped[int]
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    alignment_report_id: Mapped[UUID | None] = mapped_column(ForeignKey("alignment_reports.id"))


class PlanMembership(Base):
    __tablename__ = "plan_memberships"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_version_id", "owner_id"],
            ["learning_plan_versions.id", "learning_plan_versions.owner_id"],
        ),
        ForeignKeyConstraint(
            ["objective_version_id", "owner_id"],
            ["learning_objective_versions.id", "learning_objective_versions.owner_id"],
        ),
        UniqueConstraint("plan_version_id", "position"),
    )
    plan_version_id: Mapped[UUID] = mapped_column(primary_key=True)
    objective_version_id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID]
    position: Mapped[int]


class ArtifactDependency(Base):
    __tablename__ = "artifact_dependencies"
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    consumer_kind: Mapped[str] = mapped_column(String(50), primary_key=True)
    consumer_id: Mapped[UUID] = mapped_column(primary_key=True)
    input_kind: Mapped[str] = mapped_column(String(50), primary_key=True)
    input_id: Mapped[UUID] = mapped_column(primary_key=True)
    needs_review: Mapped[bool] = mapped_column(default=False)


class EvidenceDeletion(Timestamped, Base):
    __tablename__ = "evidence_deletions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    evidence_id: Mapped[UUID] = mapped_column(ForeignKey("personal_evidence.id"))
    status: Mapped[str] = mapped_column(String(20), default="revoked")


class Repository(Timestamped, Base):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("id", "owner_id"),
        UniqueConstraint("owner_id", "github_url", "requested_ref"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    github_url: Mapped[str] = mapped_column(String(500))
    requested_ref: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(default=1)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RepositorySnapshot(Timestamped, Base):
    __tablename__ = "repository_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repository_id", "owner_id"], ["repositories.id", "repositories.owner_id"]
        ),
        UniqueConstraint("id", "owner_id"),
        UniqueConstraint("repository_id", "commit_sha", "manifest_hash"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    repository_id: Mapped[UUID]
    commit_sha: Mapped[str] = mapped_column(String(40))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class RepositoryFile(Base):
    __tablename__ = "repository_files"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "owner_id"],
            ["repository_snapshots.id", "repository_snapshots.owner_id"],
        ),
        UniqueConstraint("snapshot_id", "path"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    snapshot_id: Mapped[UUID]
    path: Mapped[str] = mapped_column(String(1000))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    size: Mapped[int]
    parse_status: Mapped[str] = mapped_column(String(30), default="not_indexed")


class CodeCitation(Timestamped, Base):
    __tablename__ = "code_citations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "owner_id"],
            ["repository_snapshots.id", "repository_snapshots.owner_id"],
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    snapshot_id: Mapped[UUID]
    path: Mapped[str] = mapped_column(String(1000))
    start_line: Mapped[int]
    end_line: Mapped[int]
    content_hash: Mapped[str] = mapped_column(String(64))


class ProjectRecommendation(Timestamped, Base):
    __tablename__ = "project_recommendations"
    __table_args__ = (
        UniqueConstraint("id", "owner_id"),
        ForeignKeyConstraint(
            ["snapshot_id", "owner_id"],
            ["repository_snapshots.id", "repository_snapshots.owner_id"],
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    goal_version_id: Mapped[UUID] = mapped_column(ForeignKey("career_goal_versions.id"))
    snapshot_id: Mapped[UUID]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ProjectSelection(Timestamped, Base):
    __tablename__ = "project_selections"
    __table_args__ = (
        UniqueConstraint("id", "owner_id"),
        ForeignKeyConstraint(
            ["recommendation_id", "owner_id"],
            ["project_recommendations.id", "project_recommendations.owner_id"],
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    recommendation_id: Mapped[UUID]
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("repository_snapshots.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ArtifactFeedback(Timestamped, Base):
    __tablename__ = "artifact_feedback"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    artifact_kind: Mapped[str] = mapped_column(String(50))
    artifact_id: Mapped[UUID]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class LearningUnit(Timestamped, Base):
    __tablename__ = "learning_units"
    __table_args__ = (UniqueConstraint("id", "owner_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    selection_id: Mapped[UUID] = mapped_column(ForeignKey("project_selections.id"))
    objective_version_id: Mapped[UUID] = mapped_column(ForeignKey("learning_objective_versions.id"))
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("repository_snapshots.id"))
    supersedes_unit_id: Mapped[UUID | None] = mapped_column(ForeignKey("learning_units.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class LearningProgress(Base):
    __tablename__ = "learning_progress"
    unit_id: Mapped[UUID] = mapped_column(ForeignKey("learning_units.id"), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    revision: Mapped[int] = mapped_column(default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class TaskDraft(Timestamped, Base):
    __tablename__ = "task_drafts"
    __table_args__ = (UniqueConstraint("id", "owner_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    unit_id: Mapped[UUID] = mapped_column(ForeignKey("learning_units.id"))
    objective_version_id: Mapped[UUID] = mapped_column(ForeignKey("learning_objective_versions.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class BuildProject(Timestamped, Base):
    __tablename__ = "build_projects"
    __table_args__ = (UniqueConstraint("id", "owner_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class BuildTask(Timestamped, Base):
    __tablename__ = "build_tasks"
    __table_args__ = (
        UniqueConstraint("id", "owner_id"),
        UniqueConstraint("project_id", "task_draft_id"),
        ForeignKeyConstraint(
            ["project_id", "owner_id"], ["build_projects.id", "build_projects.owner_id"]
        ),
        CheckConstraint(
            "status IN ('todo','in_progress','submitted','needs_revision','accepted','cancelled')"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    project_id: Mapped[UUID]
    task_draft_id: Mapped[UUID | None] = mapped_column(ForeignKey("task_drafts.id"))
    revision: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(30), default="todo")
    current_task_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("build_task_versions.id", use_alter=True, name="fk_task_current_version")
    )
    current_submission_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("submissions.id", use_alter=True, name="fk_task_current_submission")
    )
    effective_confirmation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assessment_confirmations.id", use_alter=True, name="fk_task_confirmation")
    )


class BuildTaskVersion(Timestamped, Base):
    __tablename__ = "build_task_versions"
    __table_args__ = (
        ForeignKeyConstraint(["task_id", "owner_id"], ["build_tasks.id", "build_tasks.owner_id"]),
        UniqueConstraint("id", "owner_id"),
        UniqueConstraint("task_id", "version_no"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    task_id: Mapped[UUID]
    version_no: Mapped[int]
    objective_version_id: Mapped[UUID] = mapped_column(ForeignKey("learning_objective_versions.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Submission(Timestamped, Base):
    __tablename__ = "submissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_version_id", "owner_id"],
            ["build_task_versions.id", "build_task_versions.owner_id"],
        ),
        UniqueConstraint("id", "owner_id"),
        UniqueConstraint("task_id", "version_no"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    task_id: Mapped[UUID] = mapped_column(ForeignKey("build_tasks.id"))
    task_version_id: Mapped[UUID]
    version_no: Mapped[int]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Assessment(Timestamped, Base):
    __tablename__ = "assessments"
    __table_args__ = (UniqueConstraint("id", "owner_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    submission_id: Mapped[UUID] = mapped_column(ForeignKey("submissions.id"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    revision: Mapped[int] = mapped_column(default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class AssessmentReview(Timestamped, Base):
    __tablename__ = "assessment_reviews"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    assessment_id: Mapped[UUID] = mapped_column(ForeignKey("assessments.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class AssessmentConfirmation(Timestamped, Base):
    __tablename__ = "assessment_confirmations"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    assessment_id: Mapped[UUID] = mapped_column(ForeignKey("assessments.id"))
    review_id: Mapped[UUID] = mapped_column(ForeignKey("assessment_reviews.id"), unique=True)
    decision: Mapped[str] = mapped_column(String(30))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_reason: Mapped[str | None] = mapped_column(Text)


class PlanDeltaProposal(Timestamped, Base):
    __tablename__ = "plan_delta_proposals"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    assessment_id: Mapped[UUID] = mapped_column(ForeignKey("assessments.id"), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class MaintenanceCursor(Base):
    __tablename__ = "maintenance_cursors"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_id: Mapped[UUID | None]


class JobContractArtifact(Timestamped, Base):
    """Append-only, typed protocol objects; raw bytes are erased only by authorized maintenance."""

    __tablename__ = "job_contract_artifacts"
    __table_args__ = (
        UniqueConstraint("id", "owner_id"),
        UniqueConstraint("owner_id", "kind", "identity_key"),
        UniqueConstraint("run_id", "kind"),
        UniqueConstraint("supersedes_id"),
        ForeignKeyConstraint(
            ["parent_id", "owner_id"],
            ["job_contract_artifacts.id", "job_contract_artifacts.owner_id"],
        ),
        ForeignKeyConstraint(
            ["supersedes_id", "owner_id"],
            ["job_contract_artifacts.id", "job_contract_artifacts.owner_id"],
        ),
        ForeignKeyConstraint(["source_id", "owner_id"], ["sources.id", "sources.owner_id"]),
        ForeignKeyConstraint(
            ["source_snapshot_id", "owner_id"], ["source_snapshots.id", "source_snapshots.owner_id"]
        ),
        CheckConstraint("kind IN ('capture','snapshot','fact','report','context')"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    identity_key: Mapped[str] = mapped_column(String(128))
    parent_id: Mapped[UUID | None]
    supersedes_id: Mapped[UUID | None]
    source_id: Mapped[UUID | None]
    source_snapshot_id: Mapped[UUID | None]
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_runs.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    raw_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)


class SourceLibraryCategory(Timestamped, Base):
    __tablename__ = "source_library_categories"
    __table_args__ = (
        UniqueConstraint("id", "owner_id"),
        UniqueConstraint("owner_id", "parent_id", "name"),
        ForeignKeyConstraint(
            ["parent_id", "owner_id"],
            ["source_library_categories.id", "source_library_categories.owner_id"],
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    parent_id: Mapped[UUID | None]
    name: Mapped[str] = mapped_column(String(100))
    revision: Mapped[int] = mapped_column(default=1)


class LearningWorkspace(Timestamped, Base):
    __tablename__ = "learning_workspaces"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    revision: Mapped[int] = mapped_column(default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class CodeKnowledgeIndex(Timestamped, Base):
    __tablename__ = "code_knowledge_indexes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "owner_id"],
            ["repository_snapshots.id", "repository_snapshots.owner_id"],
        ),
        UniqueConstraint("snapshot_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    snapshot_id: Mapped[UUID]
    revision: Mapped[int] = mapped_column(default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class CodeKnowledgeChunk(Base):
    __tablename__ = "code_knowledge_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "owner_id"],
            ["repository_snapshots.id", "repository_snapshots.owner_id"],
        ),
        UniqueConstraint("snapshot_id", "ordinal"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    snapshot_id: Mapped[UUID] = mapped_column(index=True)
    ordinal: Mapped[int]
    path: Mapped[str] = mapped_column(String(1000))
    symbol: Mapped[str] = mapped_column(String(1000))
    kind: Mapped[str] = mapped_column(String(30))
    start_line: Mapped[int]
    end_line: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    search_text: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class CodeLearningLink(Timestamped, Base):
    __tablename__ = "code_learning_links"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    objective_version_id: Mapped[UUID] = mapped_column(ForeignKey("learning_objective_versions.id"))
    citation_id: Mapped[UUID] = mapped_column(ForeignKey("code_citations.id"))
    note: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("owner_id", "objective_version_id", "citation_id"),)
