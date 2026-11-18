"""Long-lived unpaid learning owner, distinct from each real Tutor AgentRun."""

from sqlalchemy import select

from ..db import locked
from ..errors import DomainError
from ..models import AgentRun, Conversation, Turn
from ..models_dialogue import WorkflowExecution, WorkflowSession
from ..util import fingerprint
from ..workflows.registry import validate_session_binding
from .learning_binding import accepted_binding_in, profile_in, require_assistance_in
from .learning_requests import receipt_in
from .runtime import TERMINAL
from .session_execution import SessionExecutions


class LearningExecutions(SessionExecutions):
    def validate_in(self, db, workflow, execution, *, access=True):
        if (
            execution is None
            or workflow.orchestration_engine != "langgraph"
            or workflow.learning_graph_version
            not in {"learning-session-v1", "learning-session-practice-v1"}
            or workflow.current_execution_id != execution.id
            or workflow.learning_graph_version != execution.graph_version
            or (
                execution.workflow_id,
                execution.conversation_id,
                execution.principal_id,
                execution.namespace_id,
            )
            != (workflow.id, workflow.conversation_id, workflow.principal_id, workflow.namespace_id)
        ):
            raise DomainError("LEARNING_BINDING", "学习执行归属或版本不匹配。", 409)
        validate_session_binding(execution)
        binding = execution.binding_payload
        if (
            execution.workflow_type != "learning_session"
            or binding.get("contract") != "learning-session-binding-v1"
            or execution.goal_binding_hash != fingerprint(binding)
            or binding.get("execution_id") != execution.id
            or binding.get("workflow_id") != workflow.id
            or binding.get("conversation_id") != workflow.conversation_id
            or binding.get("principal_id") != workflow.principal_id
            or binding.get("namespace_id") != workflow.namespace_id
            or execution.goal_version_id != workflow.goal_version_id
            or execution.source_jd_revision_id != workflow.source_jd_revision_id
            or (binding.get("accepted", {}).get("goal") or {}).get("goal_version_id")
            != workflow.goal_version_id
        ):
            raise DomainError("LEARNING_BINDING", "学习执行固定内容已变化。", 409)
        from .learning_binding import validate_persisted_owner_in

        validate_persisted_owner_in(db, workflow, execution, access=access)
        origin = binding.get("origin")
        if origin == "knowledge_handoff":
            if (
                execution.execution_generation != 2
                or not execution.predecessor_execution_id
                or execution.admission_receipt_id
                or execution.admission_action_index is not None
            ):
                raise DomainError("LEARNING_ORIGIN", "知识后继来源形状无效。", 409)
            predecessor = db.get(WorkflowExecution, execution.predecessor_execution_id)
            if access:
                self.validate_predecessor_in(db, workflow, predecessor)
            if binding.get("predecessor") != self.predecessor_binding(predecessor):
                raise DomainError("LEARNING_ORIGIN", "知识完成见证已变化。", 409)
        elif origin == "accepted_plan_admission":
            if (
                execution.execution_generation != 1
                or execution.predecessor_execution_id
                or not execution.admission_receipt_id
                or execution.admission_action_index is None
                or "predecessor" in binding
            ):
                raise DomainError("LEARNING_ORIGIN", "新采纳计划准入来源形状无效。", 409)
            initial = self.app.learning_requests.initial_in(db, workflow)
            if not initial or (initial.action_receipt_id, initial.action_index) != (
                execution.admission_receipt_id,
                execution.admission_action_index,
            ):
                raise DomainError("LEARNING_ORIGIN", "新准入缺少真实动作来源。", 409)
            receipt = receipt_in(db, initial)
            if (
                receipt.result["action_receipts"][initial.action_index]["action_type"]
                != "start_learning_session"
            ):
                raise DomainError("LEARNING_ORIGIN", "新准入动作不匹配。", 409)
        else:
            raise DomainError("LEARNING_ORIGIN", "未知学习准入来源。", 409)
        if access:
            conv = db.get(Conversation, workflow.conversation_id)
            accepted = binding["accepted"]
            current = accepted_binding_in(
                self.app,
                db,
                workflow.principal_id,
                conv,
                accepted["plan_id"],
                accepted["version_id"],
                accepted["plan_revision"],
                require_current=False,
            )
            if current != accepted:
                raise DomainError("LEARNING_PLAN_CHANGED", "学习的已采纳计划已变化。", 409)

    def validate_predecessor_in(self, db, workflow, predecessor):
        service = self.app.session_workflows
        if (
            not predecessor
            or predecessor.workflow_id != workflow.id
            or predecessor.workflow_type != "knowledge_session"
            or predecessor.execution_generation != 1
            or predecessor.status != "completed"
            or not predecessor.terminal_checkpoint_id
            or predecessor.graph_version != "knowledge-session-v2"
            or (
                predecessor.principal_id,
                predecessor.namespace_id,
                predecessor.conversation_id,
                predecessor.goal_version_id,
            )
            != (
                workflow.principal_id,
                workflow.namespace_id,
                workflow.conversation_id,
                workflow.goal_version_id,
            )
        ):
            raise DomainError("LEARNING_PREDECESSOR", "必须有真实完成的知识执行。", 409)
        binding = predecessor.binding_payload
        if (
            binding
            != service.binding(
                db, workflow, binding.get("admitted_scope"), graph_version=predecessor.graph_version
            )
            or fingerprint(binding) != predecessor.goal_binding_hash
            or not service.acceptance_in(db, workflow, predecessor)
        ):
            raise DomainError("LEARNING_PREDECESSOR", "知识完成来源或采纳不一致。", 409)

    @staticmethod
    def predecessor_binding(predecessor):
        return {
            "execution_id": predecessor.id,
            "graph_version": predecessor.graph_version,
            "state_schema_version": predecessor.state_schema_version,
            "binding_hash": predecessor.goal_binding_hash,
            "terminal_checkpoint_id": predecessor.terminal_checkpoint_id,
            "acceptance_receipt_id": predecessor.acceptance_receipt_id,
            "acceptance_action_index": predecessor.acceptance_action_index,
        }

    def create_in(
        self, db, conv, workflow, accepted, *, predecessor=None, receipt=None, index=None
    ):
        from ..util import uid

        identifier = uid()
        binding = {
            "contract": "learning-session-binding-v1",
            "origin": "knowledge_handoff" if predecessor else "accepted_plan_admission",
            "workflow_id": workflow.id,
            "execution_id": identifier,
            "conversation_id": conv.id,
            "principal_id": workflow.principal_id,
            "namespace_id": workflow.namespace_id,
            "accepted": accepted,
            "learning_graph_version": workflow.learning_graph_version,
            "tutor_graph_version": "learning-tutor-v1",
        }
        if predecessor:
            binding["predecessor"] = self.predecessor_binding(predecessor)
        else:
            binding["admission"] = {"receipt_id": receipt.id, "action_index": index}
        execution = WorkflowExecution(
            id=identifier,
            workflow_id=workflow.id,
            conversation_id=conv.id,
            principal_id=workflow.principal_id,
            namespace_id=workflow.namespace_id,
            workflow_type="learning_session",
            graph_version=workflow.learning_graph_version,
            execution_generation=2 if predecessor else 1,
            predecessor_execution_id=predecessor.id if predecessor else None,
            admission_receipt_id=receipt.id if receipt else None,
            admission_action_index=index,
            goal_version_id=workflow.goal_version_id,
            source_jd_revision_id=workflow.source_jd_revision_id,
            goal_binding_hash=fingerprint(binding),
            binding_payload=binding,
        )
        db.add(execution)
        db.flush()
        workflow.current_execution_id = execution.id
        workflow.milestone = "tutoring"
        workflow.revision += 1
        self.app.workflows.emit_in(
            db,
            conv,
            "workflow.learning_started",
            "learning-started:" + execution.id,
            workflow,
            execution_id=execution.id,
            origin=binding["origin"],
        )
        return execution

    def handoff_in(self, db, conv, workflow, predecessor):
        if (
            workflow.learning_graph_version
            not in {"learning-session-v1", "learning-session-practice-v1"}
            or workflow.current_execution_id != predecessor.id
        ):
            return None
        initial = self.app.learning_requests.initial_in(db, workflow)
        if initial is None or initial.status in {"cancelled", "rejected"}:
            return None
        if workflow.control_state != "active":
            return None
        command = self.app.workflow_controls.current(db, workflow)
        if command and command.status != "applied":
            return None
        self.validate_predecessor_in(db, workflow, predecessor)
        receipt_in(db, initial)
        if initial.predecessor_execution_id != predecessor.id:
            raise DomainError("LEARNING_PREDECESSOR", "首次请求知识来源不匹配。", 409)
        # A genuine knowledge END remains a genuine predecessor even when the
        # user's selection moved after acceptance. Verify immutable authority,
        # never rebind the original question or manufacture another acceptance.
        accepted = initial.request["accepted"]
        current = accepted_binding_in(
            self.app,
            db,
            workflow.principal_id,
            conv,
            accepted["plan_id"],
            accepted["version_id"],
            accepted["plan_revision"],
            require_current=False,
        )
        if current != accepted:
            raise DomainError("LEARNING_BINDING_CHANGED", "原采纳的固定合同已变化。", 409)
        execution = self.create_in(db, conv, workflow, accepted, predecessor=predecessor)
        initial.execution_id = execution.id
        initial.status = "ready" if initial.authorization_hash else "pending_authorization"
        try:
            self.app.learning_requests.validate_scope_in(db, conv, workflow, initial)
        except DomainError as exc:
            initial.status, initial.failure_code = "rejected", exc.code
            execution.last_error_code = exc.code
            self.app.workflows.emit_in(
                db,
                conv,
                "workflow.tutor_rejected",
                "tutor-rejected:" + initial.id,
                workflow,
                request_id=initial.id,
                code=exc.code,
            )
        self.wake_in(db, workflow, execution, "handoff")
        return execution

    def admit_accepted_in(self, db, actor, conv, receipt, index, value, message):
        if self.app.settings.learning_graph_version not in {
            "learning-session-v1",
            "learning-session-practice-v1",
        }:
            raise DomainError("LEARNING_ADMISSION_DISABLED", "新学习图准入未启用。", 409)
        if value.authorization_ref:
            self.app.dialogue._funding_in(db, actor, conv.namespace_id, value.authorization_ref)
            from ..models import FundingAgreement

            locked(db, FundingAgreement, value.authorization_ref)
        require_assistance_in(db, actor)
        self.app.practice_workflows.require_no_active_in(db, conv.id)
        accepted = accepted_binding_in(
            self.app,
            db,
            actor,
            conv,
            value.plan_id,
            value.version_id,
            value.expected_plan_revision,
            module_id=value.module_id,
        )
        profile = profile_in(db, actor)
        if profile.revision != value.expected_profile_revision:
            raise DomainError("REVISION_CONFLICT", "学习档案已变化。", 409)
        # A new key does not bypass current acceptance checks or silently replace a question.
        unresolved_history = False
        for old in db.scalars(
            select(WorkflowSession).where(
                WorkflowSession.conversation_id == conv.id,
                WorkflowSession.learning_graph_version.in_(
                    ["learning-session-v1", "learning-session-practice-v1"]
                ),
            )
        ):
            current = self.current(db, old)
            from .run_pause import _uncertain

            # Cancelling a pre-Run ledger or a child does not resolve an unknown
            # external result. An explicit fresh task must not overtake it.
            unresolved_history = unresolved_history or any(
                run.status not in TERMINAL or _uncertain(db, run)
                for run in [*self.runs(db, old, "tutor"), *self.runs(db, old, "assessment")]
            )
            if (
                current
                and current.workflow_type == "learning_session"
                and current.binding_payload["accepted"]["plan_id"] == value.plan_id
                and old.control_state in {"pause_requested", "paused", "resume_requested"}
            ):
                raise DomainError(
                    "LEARNING_ALREADY_ADMITTED", "请恢复原学习任务，不能以新准入绕过暂停。", 409
                )
            if (
                current
                and current.workflow_type == "learning_session"
                and current.binding_payload["accepted"] == accepted
                and current.graph_version == self.app.settings.learning_graph_version
            ):
                # Closed safe histories do not permanently prevent a new,
                # explicitly admitted task. Pause is not closure or permission
                # to restart; source-workflow controls are checked below too.
                if old.control_state == "cancelled" or current.status in {
                    "failed",
                    "cancelled",
                    "completed",
                }:
                    continue
                self.validate_in(db, old, current)
                request = self.app.learning_requests.initial_in(db, old)
                options = value.tutor_request
                from .learning_requests import DEFAULT_QUESTION

                if (
                    old.control_state != "active"
                    or request.request["question"] != (options.question or DEFAULT_QUESTION)
                    or request.request["model_call_cap"] != options.model_call_cap
                    or request.request["preferences"] != options.preferences.model_dump(mode="json")
                    or request.request["requested_funding_id"] != value.authorization_ref
                    or request.request["module_id"] != value.module_id
                ):
                    raise DomainError(
                        "LEARNING_ALREADY_ADMITTED",
                        "已存在学习准入；请使用明确追问或取消原任务。",
                        409,
                    )
                return old, {
                    **self.app.learning_requests.response(request),
                    "status": "already_applied",
                    "execution_id": current.id,
                }
        if unresolved_history:
            raise DomainError("LEARNING_ORIGIN_PENDING", "原导师运行或外部结果尚未解决。", 409)
        if self.app.learning_requests.pending_in(db, conv.id):
            raise DomainError("LEARNING_REQUEST_PENDING", "当前会话已有学习请求。", 409)
        scope = accepted.get("dialogue_scope")
        if scope:
            origin = db.get(WorkflowSession, scope.get("workflow_id"))
            if not origin or origin.conversation_id != conv.id or origin.control_state != "active":
                raise DomainError("LEARNING_ORIGIN", "原目标会话不可用于新准入。", 409)
            previous = self.current(db, origin)
            if previous and (previous.status != "completed" or origin.learning_graph_version):
                raise DomainError("LEARNING_ORIGIN_PENDING", "原图恢复或交接尚未解决。", 409)
            from .run_pause import _uncertain

            if any(
                run.status not in TERMINAL or _uncertain(db, run)
                for run in [*self.runs(db, origin, "tutor"), *self.runs(db, origin, "assessment")]
            ):
                raise DomainError("LEARNING_ORIGIN_PENDING", "原导师运行尚未解决。", 409)
        workflow = self.app.workflows.create_in(
            db,
            conv,
            target={
                "entry": "accepted_plan_admission",
                "accepted_plan": {
                    "plan_id": value.plan_id,
                    "active_version_id": value.version_id,
                    "revision": value.expected_plan_revision,
                },
            },
        )
        workflow.goal_version_id = (accepted.get("goal") or {}).get("goal_version_id")
        workflow.source_jd_revision_id = (
            origin.source_jd_revision_id if scope else accepted.get("source_jd_revision_id")
        )
        execution = self.create_in(db, conv, workflow, accepted, receipt=receipt, index=index)
        # Recording does not validate an origin receipt before its atomic indexed outcome exists.
        request = self.app.learning_requests.record_in(
            db, conv, workflow, receipt, index, message, initial=True
        )
        return workflow, {
            **self.app.learning_requests.response(request),
            "execution_id": execution.id,
        }

    def route_in(self, db, workflow, execution):
        if execution.graph_version == "learning-session-practice-v1":
            route = self.app.practice_workflows.route_in(db, workflow, execution)
            if route:
                return route
        pending = [
            r
            for r in self.app.learning_requests.pending_in(db, workflow.conversation_id)
            if r.workflow_id == workflow.id
        ]
        if not pending:
            from ..models import LearningPlan
            from ..models_learning_ext import LearningProfile

            accepted = execution.binding_payload["accepted"]
            plan = db.get(LearningPlan, accepted["plan_id"])
            profile = db.scalar(
                select(LearningProfile).where(LearningProfile.principal_id == workflow.principal_id)
            )
            if (
                plan.active_version_id != accepted["version_id"]
                or plan.revision != accepted["plan_revision"]
                or profile.active_plan_id != plan.id
                or profile.active_module_id not in accepted["module_ids"]
            ):
                return "wait_selection"
            return "wait_followup"
        request = pending[0]
        if request.execution_id != execution.id:
            raise DomainError("LEARNING_REQUEST_BINDING", "请求不属于当前学习执行。", 409)
        if request.status == "pending_authorization":
            return "wait_authorization"
        if request.status == "ready":
            return "dispatch_turn"
        if request.status == "dispatched":
            run = db.get(AgentRun, request.run_id)
            if not run:
                raise DomainError("LEARNING_REQUEST_BINDING", "付费运行缺失。", 409)
            from .run_pause import _uncertain

            if _uncertain(db, run):
                return "wait_reconciliation"
            return "reconcile_turn" if run.status in TERMINAL else "wait_turn"
        raise DomainError("LEARNING_REQUEST_STATE", "请求状态不兼容。", 409)

    def operate_in(self, db, conv, workflow, execution, stage):
        if stage in {
            "create_practice",
            "expire_assessment",
            "dispatch_assessment",
            "reconcile_assessment",
        }:
            if stage == "create_practice":
                try:
                    with db.begin_nested():
                        self.app.practice_workflows.operate_in(db, conv, workflow, execution, stage)
                except DomainError as exc:
                    if exc.code == "PRACTICE_BINDING":
                        raise
                    practice = self.app.practice_workflows.active_in(db, conv.id)
                    practice.status, practice.failure_code, practice.active_conversation_id = (
                        "rejected",
                        exc.code,
                        None,
                    )
                    self.app.practice_workflows.emit_in(
                        db, conv, workflow, practice, "rejected", code=exc.code
                    )
                return
            return self.app.practice_workflows.operate_in(db, conv, workflow, execution, stage)
        request = next(
            r
            for r in self.app.learning_requests.pending_in(db, conv.id)
            if r.workflow_id == workflow.id
        )
        if stage == "dispatch_turn":
            try:
                with db.begin_nested():
                    self.app.learning_requests.dispatch_in(db, conv, workflow, execution, request)
            except DomainError as exc:
                request.status, request.failure_code = "rejected", exc.code
                execution.last_error_code = exc.code
                self.app.workflows.emit_in(
                    db,
                    conv,
                    "workflow.tutor_rejected",
                    "tutor-rejected:" + request.id,
                    workflow,
                    request_id=request.id,
                    code=exc.code,
                )
        elif stage == "reconcile_turn":
            run = db.get(AgentRun, request.run_id)
            turn = db.get(Turn, request.turn_id)
            if not turn or turn.run_id != run.id or turn.conversation_id != conv.id:
                raise DomainError("LEARNING_REQUEST_BINDING", "导师结果归属不匹配。", 409)
            request.status = (
                "completed" if run.status in {"succeeded", "partially_succeeded"} else "rejected"
            )
            request.failure_code = (
                None if request.status == "completed" else "TUTOR_" + run.status.upper()
            )
            workflow.target = {
                **workflow.target,
                "last_tutor_turn_id": turn.id,
                "waiting_user_action": "followup",
            }
            self.app.workflows.emit_in(
                db,
                conv,
                "workflow.tutor_resolved",
                "tutor-resolved:" + request.id,
                workflow,
                request_id=request.id,
                turn_id=turn.id,
                run_id=run.id,
                outcome=run.status,
            )
