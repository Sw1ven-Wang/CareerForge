"""Short-lived ownership of a long-lived session graph; no AgentRun budget borrowing."""

from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import select

from ..db import aware, clock, locked
from ..errors import DomainError, LeaseLost, PauseBoundary
from ..lease_heartbeat import LeaseHeartbeats
from ..models import LearningPlan, ObjectRow, Outbox, Principal
from ..models_dialogue import (
    ConversationActionReceipt,
    GoalAddendum,
    WorkflowExecution,
    WorkflowPlanRequest,
    WorkflowRunLink,
    WorkflowSession,
)
from ..util import fingerprint
from ..workflows.registry import validate_session_binding
from .research_coverage import read_goal_version
from .runtime import TERMINAL
from .workflow_continuation import _result, _runs_for, _wait

SESSION_TERMINAL = {"completed", "failed", "cancelled"}
PLAN_AUTH_ERRORS = {
    "BUDGET_EXHAUSTED",
    "FUNDING_UNAVAILABLE",
    "EGRESS_OR_ROLE_DENIED",
    "FUNDING_SOURCE_SCOPE",
}


class SessionExecutions:
    def __init__(self, app):
        self.app, self.db = app, app.db
        self.heartbeats = LeaseHeartbeats()

    def current(self, db, workflow):
        if workflow.orchestration_engine == "legacy":
            return None
        if workflow.orchestration_engine != "langgraph":
            raise DomainError("GRAPH_ENGINE", "未知的会话执行引擎。", 409)
        execution = (
            db.get(WorkflowExecution, workflow.current_execution_id)
            if workflow.current_execution_id
            else None
        )
        if execution and execution.workflow_id != workflow.id:
            raise DomainError("GRAPH_STATE", "会话执行归属不匹配。", 409)
        return execution

    def binding(self, db, workflow, scope, *, graph_version=None):
        addendum_id = workflow.target.get("goal_addendum_id")
        addendum = db.get(GoalAddendum, addendum_id) if addendum_id else None
        if addendum_id and (
            addendum is None
            or addendum.workflow_id != workflow.id
            or addendum.author_id != workflow.principal_id
            or addendum.manifest_hash != fingerprint(addendum.manifest)
        ):
            raise DomainError("WORKFLOW_GOAL_SCOPE", "增补目标不匹配。", 409)
        binding = {
            "contract": "knowledge-session-binding-v1",
            "workflow_id": workflow.id,
            "conversation_id": workflow.conversation_id,
            "principal_id": workflow.principal_id,
            "namespace_id": workflow.namespace_id,
            "goal_version_id": workflow.goal_version_id,
            "source_jd_revision_id": workflow.source_jd_revision_id,
            "goal_addendum_id": addendum_id,
            "goal_addendum_manifest_hash": addendum.manifest_hash if addendum else None,
            "entry": workflow.target.get("entry"),
            "target": workflow.target.get("target"),
            "user_topics": list(workflow.target.get("user_topics", [])),
            "admitted_scope": scope,
        }
        graph_version = graph_version or workflow.session_graph_version
        if workflow.target.get("learning_language_profile_id"):
            from .learning_language import read_profile_in

            language_profile = read_profile_in(db, workflow.principal_id, workflow)
            binding["learning_language_profile_id"] = workflow.target[
                "learning_language_profile_id"
            ]
            binding["learning_language_profile_hash"] = fingerprint(language_profile)
        if graph_version == "knowledge-session-v2":
            binding["contract"] = "knowledge-session-binding-v2"
            binding["session_graph_version"] = graph_version
            if workflow.goal_version_id:
                obj = db.get(ObjectRow, workflow.goal_version_id)
                if obj is None:
                    raise DomainError("GOAL_HASH", "缺少冻结目标对象。", 409)
                binding["goal_object_hash"] = obj.content_hash
            else:
                binding["goal_object_hash"] = None
        elif graph_version != "knowledge-session-v1":
            raise DomainError("GRAPH_VERSION", "会话图版本未注册。", 409)
        return binding

    def admit_in(self, db, workflow, scope):
        if workflow.orchestration_engine == "legacy":
            return None
        current = self.current(db, workflow)
        binding = self.binding(db, workflow, scope)
        if current:
            if current.goal_binding_hash != fingerprint(binding):
                raise DomainError("WORKFLOW_GOAL_SCOPE", "已确认的执行范围不可改写。", 409)
            self.wake_in(db, workflow, current, "admission")
            return current
        execution = WorkflowExecution(
            workflow_id=workflow.id,
            conversation_id=workflow.conversation_id,
            principal_id=workflow.principal_id,
            namespace_id=workflow.namespace_id,
            goal_version_id=workflow.goal_version_id,
            source_jd_revision_id=workflow.source_jd_revision_id,
            goal_addendum_id=workflow.target.get("goal_addendum_id"),
            binding_payload=binding,
            goal_binding_hash=fingerprint(binding),
            graph_version=workflow.session_graph_version,
        )
        db.add(execution)
        db.flush()
        workflow.current_execution_id = execution.id
        self.wake_in(db, workflow, execution, "admission")
        return execution

    def validate_in(self, db, workflow, execution, *, access=True):
        if execution is not None and execution.workflow_type == "evolution_session":
            return self.app.evolution_workflows.validate_in(db, workflow, execution, access=access)
        if execution is not None and execution.workflow_type == "learning_session":
            return self.app.learning_workflows.validate_in(db, workflow, execution, access=access)
        if (
            execution is None
            or workflow.orchestration_engine != "langgraph"
            or workflow.current_execution_id != execution.id
            or execution.execution_generation != 1
            or (
                execution.workflow_id,
                execution.conversation_id,
                execution.principal_id,
                execution.namespace_id,
            )
            != (workflow.id, workflow.conversation_id, workflow.principal_id, workflow.namespace_id)
        ):
            raise DomainError("GRAPH_VERSION", "会话执行版本或归属不兼容。", 409)
        validate_session_binding(execution)
        binding = execution.binding_payload
        if not isinstance(binding, dict) or not isinstance(binding.get("admitted_scope"), dict):
            raise DomainError("GRAPH_STATE", "会话执行范围缺失。", 409)
        if (
            self.binding(
                db, workflow, binding["admitted_scope"], graph_version=execution.graph_version
            )
            != binding
            or fingerprint(binding) != execution.goal_binding_hash
        ):
            raise DomainError("WORKFLOW_GOAL_SCOPE", "执行的冻结目标发生变化。", 409)
        if (
            execution.goal_version_id,
            execution.source_jd_revision_id,
            execution.goal_addendum_id,
        ) != (
            binding["goal_version_id"],
            binding["source_jd_revision_id"],
            binding["goal_addendum_id"],
        ):
            raise DomainError("GRAPH_STATE", "执行的目标引用不匹配。", 409)
        if not access:
            return
        if workflow.goal_version_id:
            read_goal_version(db, workflow.principal_id, workflow.goal_version_id)
        if workflow.source_jd_revision_id:
            self.app.jd.read_in(db, workflow.principal_id, workflow.source_jd_revision_id)
        # Build roots fence their own source access. Session aggregation and
        # plan acceptance validate only the reviewed evidence actually used;
        # one revoked/failed source must not erase another source's valid partial evidence.

    def wake_in(self, db, workflow, execution=None, kind="domain"):
        execution = execution or self.current(db, workflow)
        if execution is None or execution.status in SESSION_TERMINAL:
            return
        execution = locked(db, WorkflowExecution, execution.id)
        # The cached status may predate an error completion on another worker.
        if execution is None or execution.status in SESSION_TERMINAL:
            return
        self.validate_in(db, workflow, execution, access=False)
        execution.wake_generation += 1
        execution.revision += 1
        controlling = workflow.control_generation > 0 and workflow.control_state in {
            "pause_requested",
            "resume_requested",
        }
        if execution.status != "running":
            execution.status = (
                "queued" if workflow.control_state == "active" or controlling else "paused"
            )
        if workflow.control_state == "active" or controlling:
            db.add(
                Outbox(
                    kind="workflow.execute",
                    aggregate_id=execution.id,
                    payload={"generation": execution.wake_generation, "kind": kind},
                )
            )

    def record_action_in(self, db, conv, workflow, receipt, index, action, message, result):
        if workflow is None or workflow.orchestration_engine != "langgraph":
            return
        execution = self.current(db, workflow)
        if execution is None:
            return
        plan_request = (
            action == "propose_plan"
            and result.get("status") != "waiting_confirmation"
            and not result.get("run_refs")
        )
        acceptance = action == "accept_plan" and result.get("version_id")
        if not plan_request and not acceptance:
            return
        execution = locked(db, WorkflowExecution, execution.id)
        if execution is None or execution.status in SESSION_TERMINAL:
            raise DomainError("TERMINAL_WORKFLOW", "会话执行已结束，不能登记新的请求。", 409)
        self.validate_in(db, workflow, execution, access=False)
        if plan_request:
            payload = {
                "execution_id": execution.id,
                "funding_id": message.authorization_ref,
                "binding": execution.goal_binding_hash,
            }
            db.add(
                WorkflowPlanRequest(
                    execution_id=execution.id,
                    action_receipt_id=receipt.id,
                    action_index=index,
                    funding_id=message.authorization_ref,
                    goal_binding_hash=execution.goal_binding_hash,
                    request_hash=fingerprint(payload),
                )
            )
            self.wake_in(db, workflow, execution, "plan_request")
        elif action == "accept_plan" and result.get("version_id"):
            execution.acceptance_receipt_id = receipt.id
            execution.acceptance_action_index = index
            self.wake_in(db, workflow, execution, "plan_accepted")

    def pause_in(self, db, workflow):
        execution = self.current(db, workflow)
        execution = locked(db, WorkflowExecution, execution.id) if execution else None
        if execution and execution.status not in SESSION_TERMINAL and execution.status != "running":
            execution.status = "paused"
            execution.wait_kind = "control"
            execution.revision += 1

    def active(self, db, workflow):
        execution = self.current(db, workflow)
        return bool(execution and execution.status not in SESSION_TERMINAL)

    def claim(self, identifier, worker):
        with self.db.transaction() as db:
            execution = locked(db, WorkflowExecution, identifier)
            if execution is None or execution.status != "queued":
                return None
            now = clock(db)
            execution.status = "running"
            execution.epoch += 1
            execution.revision += 1
            execution.worker_id = worker
            execution.lease_until = now + timedelta(seconds=60)
            return execution.epoch, execution.wake_generation

    def leased(self, db, identifier, epoch, worker):
        row = locked(db, WorkflowExecution, identifier)
        if (
            row is None
            or row.status != "running"
            or row.epoch != epoch
            or row.worker_id != worker
            or row.lease_until is None
            or aware(row.lease_until) <= clock(db)
        ):
            raise LeaseLost()
        return row

    @contextmanager
    def transaction(self, identifier, epoch, worker, *, control=True):
        with self.db.read() as db:
            identity = db.get(WorkflowExecution, identifier)
            if identity is None:
                raise DomainError("GRAPH_STATE", "会话执行不存在。", 404)
        with self.db.transaction() as db:
            locked(db, Principal, identity.principal_id)
            conv = self.app.workflows.conversation(
                db, identity.principal_id, identity.conversation_id, lock=True
            )
            workflow = self.app.workflows.owned_in(db, conv, identity.workflow_id)
            workflow = locked(db, WorkflowSession, workflow.id)
            execution = self.leased(db, identifier, epoch, worker)
            if execution.workflow_type == "learning_session":
                self.app.learning_requests.lock_dispatch_funding_in(db, workflow)
                self.app.practice_workflows.lock_dispatch_funding_in(db, workflow)
            self.validate_in(db, workflow, execution)
            if control and workflow.control_state != "active":
                raise PauseBoundary()
            yield db, conv, workflow, execution
            # Fenced mutations may deliberately release their lease. Operations
            # retaining ownership must still be live after domain lock waits.
            if execution.status == "running":
                self.leased(db, identifier, epoch, worker)

    def heartbeat(self, identifier, epoch, worker):
        with self.db.transaction() as db:
            execution = self.leased(db, identifier, epoch, worker)
            execution.lease_until = clock(db) + timedelta(seconds=60)

    def project_wait(self, identifier, epoch, worker, generation, kind):
        with self.transaction(identifier, epoch, worker, control=False) as (db, _, workflow, row):
            self.leased(db, identifier, epoch, worker)
            row.handled_generation = max(row.handled_generation, generation)
            row.wait_kind = kind
            if workflow.control_state != "active":
                if workflow.control_generation > 0 and workflow.control_state in {
                    "pause_requested",
                    "resume_requested",
                }:
                    row.status = "queued"
                    db.add(
                        Outbox(
                            kind="workflow.execute",
                            aggregate_id=row.id,
                            payload={"kind": "control_boundary"},
                        )
                    )
                else:
                    row.status = "paused"
            elif row.wake_generation > generation:
                row.status = "queued"
                db.add(
                    Outbox(
                        kind="workflow.execute", aggregate_id=row.id, payload={"kind": "raced_wake"}
                    )
                )
            else:
                row.status = "blocked" if kind in {"evidence", "plan_failure"} else "waiting"
            row.worker_id = None
            row.lease_until = None
            row.revision += 1

    def finish(self, identifier, epoch, worker, generation, error=None):
        if error is None:
            try:
                with self.transaction(identifier, epoch, worker) as (db, _, workflow, row):
                    if not self.acceptance_in(db, workflow, row):
                        raise DomainError("GRAPH_APPROVAL", "计划尚未采纳。", 409)
                    self.leased(db, identifier, epoch, worker)
                    row.handled_generation = max(row.handled_generation, generation)
                    row.status, row.wait_kind, row.last_error_code = "completed", None, None
                    row.worker_id, row.lease_until = None, None
                    row.revision += 1
            except LeaseLost:
                return
            return
        with self.db.transaction() as db:
            try:
                row = self.leased(db, identifier, epoch, worker)
            except LeaseLost:
                return
            row.status = "failed" if error else "completed"
            row.last_error_code = error
            row.handled_generation = max(row.handled_generation, generation)
            row.wait_kind = None
            row.worker_id = None
            row.lease_until = None
            row.revision += 1

    def complete_checkpoint(self, identifier, epoch, worker, generation, checkpoint_id):
        """Reconcile a verified saver completion without losing a raced pause request."""
        if not checkpoint_id:
            raise DomainError("GRAPH_STATE", "完成缺少持久检查点。", 409)
        with self.transaction(identifier, epoch, worker, control=False) as (
            db,
            conv,
            workflow,
            row,
        ):
            if not self.acceptance_in(db, workflow, row):
                raise DomainError("GRAPH_APPROVAL", "完成需要真实的精确计划采纳。", 409)
            row.terminal_checkpoint_id = checkpoint_id
            row.status, row.wait_kind, row.last_error_code = "completed", None, None
            row.worker_id, row.lease_until = None, None
            row.handled_generation = max(row.handled_generation, generation)
            row.revision += 1
            if workflow.control_state == "pause_requested" and workflow.control_generation > 0:
                command = self.app.workflow_controls.current(db, workflow)
                self.app.workflow_controls.checkpoint_in(
                    db, conv, workflow, row, command.id, checkpoint_id
                )
            if workflow.learning_graph_version:
                self.app.learning_workflows.handoff_in(db, conv, workflow, row)

    def reap(self):
        self.app.practice_workflows.reap_expired()
        with self.db.transaction() as db:
            for workflow in db.scalars(
                select(WorkflowSession).where(
                    WorkflowSession.learning_graph_version.in_(
                        ["learning-session-v1", "learning-session-practice-v1"]
                    ),
                    WorkflowSession.control_state == "active",
                )
            ):
                current = self.current(db, workflow)
                initial = self.app.learning_requests.initial_in(db, workflow)
                if (
                    current
                    and current.workflow_type == "knowledge_session"
                    and current.status == "completed"
                    and initial
                    and initial.status == "pending_handoff"
                ):
                    if not db.scalar(
                        select(Outbox.id)
                        .where(
                            Outbox.kind == "workflow.execute",
                            Outbox.aggregate_id == current.id,
                            Outbox.delivered.is_(False),
                        )
                        .limit(1)
                    ):
                        db.add(
                            Outbox(
                                kind="workflow.execute",
                                aggregate_id=current.id,
                                payload={"kind": "learning_handoff_recovery"},
                            )
                        )
            from ..models_dialogue import WorkflowControlCommand

            completed_controls = db.scalars(
                select(WorkflowExecution.id)
                .join(WorkflowSession, WorkflowSession.id == WorkflowExecution.workflow_id)
                .join(
                    WorkflowControlCommand, WorkflowControlCommand.workflow_id == WorkflowSession.id
                )
                .where(
                    WorkflowExecution.status == "completed",
                    WorkflowControlCommand.generation == WorkflowSession.control_generation,
                    WorkflowControlCommand.status == "requested",
                    WorkflowSession.control_state.in_(["pause_requested", "resume_requested"]),
                )
            )
            for identifier in completed_controls:
                if not db.scalar(
                    select(Outbox.id)
                    .where(
                        Outbox.kind == "workflow.execute",
                        Outbox.aggregate_id == identifier,
                        Outbox.delivered.is_(False),
                    )
                    .limit(1)
                ):
                    db.add(
                        Outbox(
                            kind="workflow.execute",
                            aggregate_id=identifier,
                            payload={"kind": "terminal_control_recovery"},
                        )
                    )
            rows = list(
                db.scalars(select(WorkflowExecution).where(WorkflowExecution.status == "running"))
            )
            for item in rows:
                row = locked(db, WorkflowExecution, item.id)
                if (
                    row.status == "running"
                    and row.lease_until
                    and aware(row.lease_until) <= clock(db)
                ):
                    row.status, row.worker_id, row.lease_until = "queued", None, None
                    row.revision += 1
                    db.add(
                        Outbox(
                            kind="workflow.execute",
                            aggregate_id=row.id,
                            payload={"kind": "lease_expired"},
                        )
                    )

    def runs(self, db, workflow, milestone):
        runs = _runs_for(db, workflow, milestone)
        for run in runs:
            link = db.scalar(select(WorkflowRunLink).where(WorkflowRunLink.run_id == run.id))
            if (
                run.parent_id
                or run.principal_id != workflow.principal_id
                or run.namespace_id != workflow.namespace_id
                or link.goal_version_id != workflow.goal_version_id
            ):
                raise DomainError("WORKFLOW_RUN_SCOPE", "工作流子任务范围不匹配。", 409)
        return runs

    def pending_requests(self, db, execution):
        return list(
            db.scalars(
                select(WorkflowPlanRequest)
                .where(
                    WorkflowPlanRequest.execution_id == execution.id,
                    WorkflowPlanRequest.status == "pending",
                )
                .order_by(WorkflowPlanRequest.created_at, WorkflowPlanRequest.id)
            )
        )

    def acceptance_in(self, db, workflow, execution):
        if execution.workflow_type == "evolution_session":
            return self.app.evolution_workflows.acceptance_in(db, workflow, execution)
        accepted, proposal = workflow.target.get("accepted_plan"), workflow.target.get("proposal")
        if not accepted:
            return False
        receipt = (
            db.get(ConversationActionReceipt, execution.acceptance_receipt_id)
            if execution.acceptance_receipt_id
            else None
        )
        if (
            not proposal
            or receipt is None
            or (
                execution.graph_version == "knowledge-session-v1"
                and receipt.workflow_id != workflow.id
            )
            or receipt.conversation_id != workflow.conversation_id
        ):
            raise DomainError("GRAPH_APPROVAL", "计划采纳缺少匹配的动作回执。", 409)
        actions = receipt.result.get("action_receipts", [])
        index = execution.acceptance_action_index
        if not isinstance(index, int) or index < 0 or index >= len(actions):
            raise DomainError("GRAPH_APPROVAL", "采纳动作回执无效。", 409)
        action = actions[index]
        if (
            action.get("action_type"),
            action.get("status"),
            action.get("workflow_id"),
            action.get("version_id"),
            action.get("proposal_run_id"),
        ) != ("accept_plan", "applied", workflow.id, proposal["version_id"], proposal["run_id"]):
            raise DomainError("GRAPH_APPROVAL", "采纳回执没有确认该计划版本。", 409)
        plans = self.runs(db, workflow, "plan")
        if (
            len(plans) != 1
            or plans[0].id != proposal["run_id"]
            or plans[0].status not in {"succeeded", "partially_succeeded"}
        ):
            raise DomainError("GRAPH_APPROVAL", "采纳的生成任务不匹配。", 409)
        result = self.app.learning.validate_plan_result(
            db, workflow.principal_id, plans[0].id, _result(db, plans[0])
        )
        if proposal != {key: result[key] for key in ("plan_id", "version_id", "revision")} | {
            "run_id": plans[0].id
        }:
            raise DomainError("GRAPH_APPROVAL", "采纳的提案不匹配。", 409)
        plan = db.get(LearningPlan, proposal["plan_id"])
        if (
            accepted.get("plan_id") != plan.id
            or accepted.get("active_version_id") != proposal["version_id"]
            or accepted.get("revision") != proposal["revision"] + 1
        ):
            raise DomainError("GRAPH_APPROVAL", "计划采纳结果不匹配。", 409)
        return True

    def route_in(self, db, workflow, execution):
        builds = self.runs(db, workflow, "build")
        if any(run.status not in TERMINAL for run in builds):
            return "wait_builds"
        if not workflow.target.get("build_results_finalized"):
            return "aggregate_builds"
        if not self.app.continuation.publications(
            db, workflow.principal_id, workflow.target.get("publication_ids", [])
        ):
            return "wait_evidence"
        plans = self.runs(db, workflow, "plan")
        if len(plans) > 1:
            raise DomainError("GRAPH_STATE", "会话包含多个计划生成任务。", 409)
        if not plans:
            if workflow.target.get(
                "waiting_user_action"
            ) == "authorize_plan" and not self.pending_requests(db, execution):
                return "wait_authorization"
            return "ensure_plan"
        plan = plans[0]
        if plan.status not in TERMINAL:
            return "wait_plan"
        if plan.status not in {"succeeded", "partially_succeeded"}:
            return "wait_plan_failure"
        if not workflow.target.get("proposal"):
            return "observe_plan"
        return "completed" if self.acceptance_in(db, workflow, execution) else "wait_approval"

    def ensure_plan_in(self, db, conv, workflow, execution):
        requests = self.pending_requests(db, execution)
        if not requests:
            try:
                with db.begin_nested():
                    self.app.continuation.propose_plan(db, workflow.principal_id, conv, workflow)
            except DomainError as exc:
                if exc.code not in PLAN_AUTH_ERRORS:
                    raise
                execution.last_error_code = exc.code
                _wait(workflow, "building", "authorize_plan")
            return
        for request in requests:
            receipt = db.get(ConversationActionReceipt, request.action_receipt_id)
            expected = fingerprint(
                {
                    "execution_id": execution.id,
                    "funding_id": request.funding_id,
                    "binding": execution.goal_binding_hash,
                }
            )
            actions = receipt.result.get("action_receipts", []) if receipt else []
            if (
                request.goal_binding_hash != execution.goal_binding_hash
                or request.request_hash != expected
                or not receipt
                or (
                    execution.graph_version == "knowledge-session-v1"
                    and receipt.workflow_id != workflow.id
                )
                or receipt.conversation_id != conv.id
                or request.action_index >= len(actions)
                or actions[request.action_index].get("action_type") != "propose_plan"
                or actions[request.action_index].get("status") != "applied"
                or actions[request.action_index].get("workflow_id") != workflow.id
            ):
                raise DomainError("GRAPH_REQUEST", "计划请求回执或范围无效。", 409)
            try:
                with db.begin_nested():
                    self.app.continuation.propose_plan(
                        db, workflow.principal_id, conv, workflow, request.funding_id
                    )
            except DomainError as exc:
                if exc.code not in PLAN_AUTH_ERRORS:
                    raise
                execution.last_error_code = exc.code
                request.status, request.failure_code = "rejected", exc.code
                _wait(workflow, "building", "authorize_plan")
                continue
            runs = self.runs(db, workflow, "plan")
            request.status = "applied" if runs else "rejected"
            if runs:
                execution.last_error_code = None
            request.plan_run_id = runs[0].id if runs else None
            if not runs:
                request.failure_code = "PLAN_AUTHORIZATION_REQUIRED"
                _wait(workflow, "building", "authorize_plan")
