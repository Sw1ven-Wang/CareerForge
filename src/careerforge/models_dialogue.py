"""Conversation control metadata. AgentRun remains the execution ledger."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, Row


class WorkflowSession(Row, Base):
    __tablename__ = "workflow_sessions"

    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    parent_workflow_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_sessions.id"))
    goal_version_id: Mapped[str | None] = mapped_column(ForeignKey("career_goal_versions.id"))
    source_jd_revision_id: Mapped[str | None] = mapped_column(ForeignKey("dependency_objects.id"))
    orchestration_engine: Mapped[str] = mapped_column(
        String(16), default="legacy", server_default="legacy"
    )
    session_graph_version: Mapped[str] = mapped_column(
        String(40), default="knowledge-session-v1", server_default="knowledge-session-v1"
    )
    learning_graph_version: Mapped[str | None] = mapped_column(String(40))
    control_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    current_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_executions.id", name="fk_workflow_current_execution", use_alter=True)
    )
    control_state: Mapped[str] = mapped_column(String(32), default="active")
    milestone: Mapped[str] = mapped_column(String(40), default="awaiting_jd")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    target: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class WorkflowRunLink(Row, Base):
    __tablename__ = "workflow_run_links"
    __table_args__ = (UniqueConstraint("workflow_id", "milestone", "ordinal"),)

    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflow_sessions.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    milestone: Mapped[str] = mapped_column(String(40))
    ordinal: Mapped[int] = mapped_column(Integer)
    goal_version_id: Mapped[str | None] = mapped_column(ForeignKey("career_goal_versions.id"))
    source_scope_hash: Mapped[str | None] = mapped_column(String(64))


class ConversationActionReceipt(Row, Base):
    __tablename__ = "conversation_action_receipts"
    __table_args__ = (UniqueConstraint("conversation_id", "idempotency_key"),)

    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    workflow_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_sessions.id"))
    idempotency_key: Mapped[str] = mapped_column(String(120))
    request_hash: Mapped[str] = mapped_column(String(64))
    action_plan_hash: Mapped[str] = mapped_column(String(64))
    plan: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any]] = mapped_column(JSON)


class ConversationActivityEvent(Row, Base):
    __tablename__ = "conversation_activity_events"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq"),
        UniqueConstraint("conversation_id", "origin_key"),
    )

    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    workflow_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_sessions.id"))
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(100))
    origin_key: Mapped[str] = mapped_column(String(140))
    reference: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class GoalAddendum(Row, Base):
    __tablename__ = "workflow_goal_addenda"
    __table_args__ = (
        UniqueConstraint(
            "command_receipt_id", "command_action_index", name="uq_goal_addendum_command"
        ),
        CheckConstraint(
            "(command_receipt_id IS NULL AND command_action_index IS NULL) OR "
            "(command_receipt_id IS NOT NULL AND command_action_index IS NOT NULL "
            "AND command_action_index >= 0)",
            name="ck_goal_addendum_command",
        ),
    )
    command_receipt_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_action_receipts.id", name="fk_addendum_command_receipt"),
        index=True,
    )
    command_action_index: Mapped[int | None] = mapped_column(Integer)

    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflow_sessions.id"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    source_goal_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("career_goal_versions.id")
    )
    parent_addendum_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_goal_addenda.id"))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)


class WorkflowExecution(Row, Base):
    """Short orchestration lease, separate from model-running AgentRun budgets."""

    __tablename__ = "workflow_executions"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id", "execution_generation", name="uq_workflow_execution_generation"
        ),
        CheckConstraint(
            "execution_generation >= 1 AND epoch >= 0 AND revision >= 1 "
            "AND wake_generation >= 0 AND handled_generation >= 0 "
            "AND handled_generation <= wake_generation",
            name="ck_workflow_execution_counters",
        ),
        CheckConstraint(
            "workflow_engine = 'langgraph' AND state_schema_version >= 1",
            name="ck_workflow_execution_engine",
        ),
        CheckConstraint(
            "acceptance_action_index IS NULL OR acceptance_action_index >= 0",
            name="ck_workflow_acceptance_index",
        ),
        UniqueConstraint("predecessor_execution_id", name="uq_learning_predecessor"),
        UniqueConstraint(
            "admission_receipt_id", "admission_action_index", name="uq_learning_admission"
        ),
        CheckConstraint(
            "(admission_receipt_id IS NULL AND admission_action_index IS NULL) OR "
            "(admission_receipt_id IS NOT NULL AND admission_action_index IS NOT NULL AND admission_action_index >= 0)",
            name="ck_learning_admission",
        ),
        Index("ix_workflow_executions_status_lease", "status", "lease_until"),
    )

    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflow_sessions.id"), index=True)
    predecessor_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_executions.id", name="fk_learning_predecessor")
    )
    admission_receipt_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_action_receipts.id", name="fk_learning_admission")
    )
    admission_action_index: Mapped[int | None] = mapped_column(Integer)
    execution_generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    workflow_type: Mapped[str] = mapped_column(
        String(40), default="knowledge_session", server_default="knowledge_session"
    )
    workflow_engine: Mapped[str] = mapped_column(
        String(16), default="langgraph", server_default="langgraph"
    )
    graph_version: Mapped[str] = mapped_column(
        String(40), default="knowledge-session-v1", server_default="knowledge-session-v1"
    )
    state_schema_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    goal_version_id: Mapped[str | None] = mapped_column(ForeignKey("career_goal_versions.id"))
    source_jd_revision_id: Mapped[str | None] = mapped_column(ForeignKey("dependency_objects.id"))
    goal_addendum_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_goal_addenda.id"))
    goal_binding_hash: Mapped[str] = mapped_column(String(64))
    binding_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="queued", server_default="queued")
    wait_kind: Mapped[str | None] = mapped_column(String(32))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    terminal_checkpoint_id: Mapped[str | None] = mapped_column(String(120))
    epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    worker_id: Mapped[str | None] = mapped_column(String(120))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wake_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    handled_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    acceptance_receipt_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_action_receipts.id")
    )
    acceptance_action_index: Mapped[int | None] = mapped_column(Integer)


class WorkflowPlanRequest(Row, Base):
    """An admitted /plan request retains its explicit funding and receipt identity."""

    __tablename__ = "workflow_plan_requests"
    __table_args__ = (
        UniqueConstraint(
            "action_receipt_id", "action_index", name="uq_workflow_plan_request_action"
        ),
        CheckConstraint("action_index >= 0", name="ck_workflow_plan_request_action_index"),
    )

    execution_id: Mapped[str] = mapped_column(ForeignKey("workflow_executions.id"), index=True)
    action_receipt_id: Mapped[str] = mapped_column(ForeignKey("conversation_action_receipts.id"))
    action_index: Mapped[int] = mapped_column(Integer)
    funding_id: Mapped[str | None] = mapped_column(ForeignKey("funding_agreements.id"))
    goal_binding_hash: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending")
    plan_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))
    failure_code: Mapped[str | None] = mapped_column(String(100))


class WorkflowControlCommand(Row, Base):
    """Receipt-derived control identity, never a model authorization or graph state."""

    __tablename__ = "workflow_control_commands"
    __table_args__ = (
        UniqueConstraint("workflow_id", "generation", name="uq_workflow_control_generation"),
        UniqueConstraint("action_receipt_id", "action_index", name="uq_workflow_control_action"),
        CheckConstraint(
            "generation >= 1 AND action_index >= 0", name="ck_workflow_control_counters"
        ),
        CheckConstraint("kind IN ('pause', 'resume', 'cancel')", name="ck_workflow_control_kind"),
    )
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflow_sessions.id"), index=True)
    execution_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_executions.id"))
    action_receipt_id: Mapped[str] = mapped_column(ForeignKey("conversation_action_receipts.id"))
    action_index: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    goal_binding_hash: Mapped[str | None] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="requested")
    checkpoint_id: Mapped[str | None] = mapped_column(String(120))
    resume_command_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_control_commands.id")
    )


class WorkflowTutorRequest(Row, Base):
    """Immutable user request; deferred funding is not a placeholder paid Turn."""

    __tablename__ = "workflow_tutor_requests"
    __table_args__ = (
        UniqueConstraint("action_receipt_id", "action_index", name="uq_tutor_request_action"),
        UniqueConstraint("workflow_id", "sequence", name="uq_tutor_request_sequence"),
        CheckConstraint("action_index >= 0 AND sequence >= 1", name="ck_tutor_request_counters"),
        CheckConstraint("kind IN ('initial','followup')", name="ck_tutor_request_kind"),
        CheckConstraint(
            "status IN ('pending_handoff','pending_authorization','ready','dispatched',"
            "'completed','rejected','cancelled')",
            name="ck_tutor_request_status",
        ),
        CheckConstraint(
            "(turn_id IS NULL AND run_id IS NULL) OR (turn_id IS NOT NULL AND run_id IS NOT NULL)",
            name="ck_tutor_request_paid",
        ),
        CheckConstraint(
            "(authorization_receipt_id IS NULL AND authorization_action_index IS NULL "
            "AND funding_id IS NULL AND authorization_hash IS NULL) OR "
            "(authorization_receipt_id IS NOT NULL AND authorization_action_index IS NOT NULL AND authorization_action_index >= 0 "
            "AND funding_id IS NOT NULL AND authorization_hash IS NOT NULL)",
            name="ck_tutor_request_authority",
        ),
    )
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflow_sessions.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    execution_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_executions.id"))
    predecessor_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_executions.id")
    )
    action_receipt_id: Mapped[str] = mapped_column(ForeignKey("conversation_action_receipts.id"))
    action_index: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    predecessor_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_tutor_requests.id")
    )
    predecessor_turn_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_turns.id"))
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    authorization_receipt_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_action_receipts.id")
    )
    authorization_action_index: Mapped[int | None] = mapped_column(Integer)
    funding_id: Mapped[str | None] = mapped_column(ForeignKey("funding_agreements.id"))
    authorization_hash: Mapped[str | None] = mapped_column(String(64))
    materialized_input: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    materialized_hash: Mapped[str | None] = mapped_column(String(64))
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_turns.id"), unique=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    failure_code: Mapped[str | None] = mapped_column(String(100))


class WorkflowPracticeRequest(Row, Base):
    __tablename__ = "workflow_practice_requests"
    __table_args__ = (
        UniqueConstraint("action_receipt_id", "action_index", name="uq_practice_origin"),
        UniqueConstraint("execution_id", "sequence", name="uq_practice_sequence"),
        CheckConstraint("sequence >= 1 AND action_index >= 0", name="ck_practice_counters"),
        CheckConstraint(
            "(active_conversation_id IS NOT NULL AND active_conversation_id = conversation_id AND status IN ('queued','waiting_answer','submitted')) OR (active_conversation_id IS NULL AND status IN ('completed','abandoned','cancelled','rejected'))",
            name="ck_practice_active",
        ),
        CheckConstraint(
            "(activity_id IS NULL AND attempt_id IS NULL AND workspace_id IS NULL AND specification_hash IS NULL) OR (activity_id IS NOT NULL AND attempt_id IS NOT NULL AND workspace_id IS NOT NULL AND specification_hash IS NOT NULL)",
            name="ck_practice_materialized",
        ),
        CheckConstraint(
            "status IN ('queued','waiting_answer','submitted','completed','abandoned','cancelled','rejected')",
            name="ck_practice_status",
        ),
    )
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    active_conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id"), unique=True
    )
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflow_sessions.id"), index=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("workflow_executions.id"), index=True)
    action_receipt_id: Mapped[str] = mapped_column(ForeignKey("conversation_action_receipts.id"))
    action_index: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer)
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), default="queued")
    activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("dependency_objects.id"), unique=True
    )
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("learning_attempts.id"), unique=True)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"), unique=True)
    specification_hash: Mapped[str | None] = mapped_column(String(64))
    submission: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    failure_code: Mapped[str | None] = mapped_column(String(80))


class WorkflowAssessmentRequest(Row, Base):
    __tablename__ = "workflow_assessment_requests"
    __table_args__ = (
        UniqueConstraint("action_receipt_id", "action_index", name="uq_assessment_origin"),
        UniqueConstraint("practice_id", "sequence", name="uq_assessment_sequence"),
        CheckConstraint("sequence >= 1 AND action_index >= 0", name="ck_assessment_counters"),
        CheckConstraint(
            "(active_practice_id IS NOT NULL AND active_practice_id = practice_id AND status IN ('pending_authorization','ready','dispatched','blocked_reconciliation')) OR (active_practice_id IS NULL AND status IN ('completed','rejected','expired','cancelled'))",
            name="ck_assessment_active",
        ),
        CheckConstraint(
            "(run_id IS NULL) OR (authorization_hash IS NOT NULL AND funding_id IS NOT NULL)",
            name="ck_assessment_run_authority",
        ),
        CheckConstraint(
            "status IN ('pending_authorization','ready','dispatched','completed','rejected','expired','cancelled','blocked_reconciliation')",
            name="ck_assessment_request_status",
        ),
        CheckConstraint(
            "(authorization_receipt_id IS NULL AND authorization_action_index IS NULL AND funding_id IS NULL AND authorization_hash IS NULL) OR (authorization_receipt_id IS NOT NULL AND authorization_action_index IS NOT NULL AND authorization_action_index >= 0 AND funding_id IS NOT NULL AND authorization_hash IS NOT NULL)",
            name="ck_assessment_authorization",
        ),
    )
    practice_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_practice_requests.id"), index=True
    )
    active_practice_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_practice_requests.id"), unique=True
    )
    action_receipt_id: Mapped[str] = mapped_column(ForeignKey("conversation_action_receipts.id"))
    action_index: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer)
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), default="pending_authorization")
    authorization_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    authorization_receipt_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_action_receipts.id")
    )
    authorization_action_index: Mapped[int | None] = mapped_column(Integer)
    funding_id: Mapped[str | None] = mapped_column(ForeignKey("funding_agreements.id"))
    authorization_hash: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    result_hash: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(80))


class WorkflowEvolutionRequest(Row, Base):
    """Narrow explicit Evolution commands, not inferred coordinator actions."""

    __tablename__ = "workflow_evolution_requests"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflow_sessions.id"), index=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("workflow_executions.id"), index=True)
    command_receipt_id: Mapped[str] = mapped_column(
        ForeignKey("idempotency_records.id"), unique=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    admission_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    funding_id: Mapped[str | None] = mapped_column(ForeignKey("funding_agreements.id"))
    authorization_receipt_id: Mapped[str | None] = mapped_column(
        ForeignKey("idempotency_records.id")
    )
    authorization_witness: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (
        UniqueConstraint("execution_id", "sequence", name="uq_evolution_request_sequence"),
        CheckConstraint("sequence >= 1", name="ck_evolution_request_sequence"),
        CheckConstraint(
            "kind IN ('candidate','replay','promote','rollback','reject')",
            name="ck_evolution_request_kind",
        ),
        CheckConstraint(
            "status IN ('authorization','queued','running','completed','failed','rejected','cancelled')",
            name="ck_evolution_request_status",
        ),
        CheckConstraint(
            "(funding_id IS NULL AND authorization_receipt_id IS NULL) OR (funding_id IS NOT NULL AND authorization_receipt_id IS NOT NULL)",
            name="ck_evolution_request_authority",
        ),
    )
