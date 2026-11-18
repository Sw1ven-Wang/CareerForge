"""Receipt-first, bounded Tutor admission. Checkpoints and wakes grant no authority."""

import re

from sqlalchemy import select

from ..contracts_conversation_actions_v3 import TutorRequestInputV1
from ..contracts_session_tutor_v166 import SessionTutorTurnInput
from ..db import aware, clock, locked
from ..errors import DomainError
from ..models import AgentRun, FundingAgreement, Turn
from ..models_dialogue import (
    ConversationActionReceipt,
    WorkflowExecution,
    WorkflowSession,
    WorkflowTutorRequest,
)
from ..util import fingerprint
from .dependencies import assert_source_scope, check_closure
from .learning_binding import accepted_binding_in, profile_in, require_assistance_in

PENDING = {"pending_handoff", "pending_authorization", "ready", "dispatched"}
AUTH_ERRORS = {
    "FUNDING_UNAVAILABLE",
    "BUDGET_EXHAUSTED",
    "EGRESS_OR_ROLE_DENIED",
    "FUNDING_SOURCE_SCOPE",
}
DEFAULT_QUESTION = "请基于已审核源码讲解当前模块的关键条件与适用边界。"
BINDING_KEY = "learning_tutor_request"


def _fail():
    raise DomainError("LEARNING_REQUEST_BINDING", "学习请求的固定身份或动作回执已变化。", 409)


def receipt_in(db, request, *, authorization=False):
    identifier = request.authorization_receipt_id if authorization else request.action_receipt_id
    index = request.authorization_action_index if authorization else request.action_index
    receipt = db.get(ConversationActionReceipt, identifier) if identifier else None
    actions = receipt.result.get("action_receipts", []) if receipt else []
    planned = receipt.plan.get("actions", []) if receipt else []
    if (
        not receipt
        or receipt.conversation_id != request.conversation_id
        or receipt.action_plan_hash != fingerprint(receipt.plan)
        or type(index) is not int
        or index < 0
        or index >= len(actions)
        or index >= len(planned)
    ):
        _fail()
    action = actions[index]
    allowed = (
        {
            "accept_plan",
            "start_learning_session",
            "learning_followup",
            "start_tutor",
            "authorize_tutor_request",
        }
        if authorization
        else {"accept_plan", "start_learning_session", "learning_followup", "start_tutor"}
    )
    if (
        action.get("status") != "applied"
        or action.get("action_type") not in allowed
        or planned[index].get("action", {}).get("type") != action.get("action_type")
        or action.get("action_index") != index
        or action.get("workflow_id") != request.workflow_id
        or action.get("request_id") != request.id
        or action.get("request_hash") != request.request_hash
    ):
        _fail()
    if not authorization and (
        request.request_hash != fingerprint(request.request)
        or request.request.get("origin_request_hash") != receipt.request_hash
    ):
        _fail()
    if authorization and (
        action.get("funding_id") != request.funding_id
        or action.get("authorization_hash") != request.authorization_hash
    ):
        _fail()
    if authorization and request.authorization_hash != fingerprint(
        {
            "request_id": request.id,
            "request_hash": request.request_hash,
            "funding_id": request.funding_id,
            "receipt_id": receipt.id,
            "action_index": index,
            "receipt_request_hash": receipt.request_hash,
        }
    ):
        _fail()
    return receipt


def binding_for(request):
    return {
        "contract": "learning-tutor-request-v1",
        "request_id": request.id,
        "execution_id": request.execution_id,
        "workflow_id": request.workflow_id,
        "request_hash": request.request_hash,
        "materialized_input_hash": request.materialized_hash,
        "authorization_hash": request.authorization_hash,
    }


def _owner_in(db, request, actor, namespace_id, *, delivered=False, allow_pause=False):
    workflow = db.get(WorkflowSession, request.workflow_id)
    execution = db.get(WorkflowExecution, request.execution_id) if request.execution_id else None
    if (
        not workflow
        or not execution
        or workflow.principal_id != actor
        or workflow.namespace_id != namespace_id
        or request.conversation_id != workflow.conversation_id
        or execution.workflow_id != workflow.id
        or workflow.current_execution_id != execution.id
        or workflow.learning_graph_version
        not in {"learning-session-v1", "learning-session-practice-v1"}
        or execution.workflow_type != "learning_session"
        or execution.graph_version != workflow.learning_graph_version
        or execution.goal_binding_hash != fingerprint(execution.binding_payload)
        or execution.binding_payload.get("accepted") != request.request.get("accepted")
    ):
        _fail()
    allowed_control = {"active", "pause_requested", "paused"} if allow_pause else {"active"}
    if workflow.control_state not in allowed_control or execution.status in {
        "failed",
        "cancelled",
        "completed",
    }:
        raise DomainError("WORKFLOW_NOT_ACTIVE", "学习工作流不能继续。", 409)
    from .learning_binding import validate_persisted_owner_in

    validate_persisted_owner_in(db, workflow, execution)
    receipt_in(db, request)
    receipt_in(db, request, authorization=True)
    if request.materialized_hash != fingerprint(request.materialized_input):
        _fail()
    return workflow, execution


def validate_run_admission_in(db, actor, namespace_id, manifest):
    binding = manifest.get(BINDING_KEY)
    if not isinstance(binding, dict):
        _fail()
    request = db.get(WorkflowTutorRequest, binding.get("request_id"))
    if (
        not request
        or request.status != "ready"
        or request.run_id
        or request.turn_id
        or binding != binding_for(request)
    ):
        _fail()
    _owner_in(db, request, actor, namespace_id)
    turn = db.get(Turn, manifest.get("turn_id"))
    if (
        not turn
        or turn.conversation_id != request.conversation_id
        or turn.question != request.materialized_input["question"]
        or turn.idempotency_key != request.materialized_input["idempotency_key"]
        or manifest.get("funding_id") != request.funding_id
        or turn.focus.get(BINDING_KEY) != binding
    ):
        _fail()
    return binding


def validate_delivered_request_in(db, run, binding, *, allow_pause=False):
    request = db.scalar(select(WorkflowTutorRequest).where(WorkflowTutorRequest.run_id == run.id))
    if (
        not request
        or binding != binding_for(request)
        or run.input_manifest.get(BINDING_KEY) != binding
        or request.turn_id != run.input_manifest.get("turn_id")
        or request.funding_id != run.funding_id
        or request.status not in {"dispatched", "completed"}
    ):
        _fail()
    _owner_in(
        db, request, run.principal_id, run.namespace_id, delivered=True, allow_pause=allow_pause
    )
    return request


class LearningRequests:
    def __init__(self, app):
        self.app, self.db = app, app.db

    def pending_in(self, db, conversation_id):
        return list(
            db.scalars(
                select(WorkflowTutorRequest)
                .where(
                    WorkflowTutorRequest.conversation_id == conversation_id,
                    WorkflowTutorRequest.status.in_(PENDING),
                )
                .order_by(WorkflowTutorRequest.sequence)
            )
        )

    def last_in(self, db, workflow):
        return db.scalar(
            select(WorkflowTutorRequest)
            .where(WorkflowTutorRequest.workflow_id == workflow.id)
            .order_by(WorkflowTutorRequest.sequence.desc())
            .limit(1)
        )

    def initial_in(self, db, workflow):
        return db.scalar(
            select(WorkflowTutorRequest).where(
                WorkflowTutorRequest.workflow_id == workflow.id,
                WorkflowTutorRequest.kind == "initial",
            )
        )

    @staticmethod
    def response(request):
        # A committed action result never changes when asynchronous dispatch happens.
        return {
            "request_id": request.id,
            "request_hash": request.request_hash,
            "request_status": request.status,
            "funding_id": request.funding_id,
            "authorization_hash": request.authorization_hash,
            "run_refs": [],
            "reply": "已登记本次学习请求。",
        }

    def funding_check_in(self, db, workflow, request, funding_id):
        funding = locked(db, FundingAgreement, funding_id) if funding_id else None
        if (
            not funding
            or funding.payer_id != workflow.principal_id
            or funding.namespace_id != workflow.namespace_id
        ):
            if funding_id:
                raise DomainError("NOT_FOUND", "找不到当前会话可用的授权。", 404)
            raise DomainError("FUNDING_UNAVAILABLE", "讲解需要明确费用授权。", 403)
        if funding.status != "active" or aware(funding.expires_at) <= clock(db):
            raise DomainError("FUNDING_UNAVAILABLE", "费用授权当前不可用。", 403)
        if (
            funding.calls_consumed + funding.calls_held + request.request["model_call_cap"]
            > funding.call_cap
        ):
            raise DomainError("BUDGET_EXHAUSTED", "没有足够额度预留完整讲解。", 409)
        closure = check_closure(db, workflow.principal_id, [request.request["module_id"]])
        if not {"tutor", "answer_checker"} <= set(funding.allowed_roles) or not {
            "learning_state",
            *closure.source_data_classes,
        } <= set(funding.allowed_data_classes):
            raise DomainError("EGRESS_OR_ROLE_DENIED", "导师角色或数据类别未授权。", 403)
        assert_source_scope(db, [request.request["module_id"]], funding.allowed_source_ids)
        return funding

    def seal_in(self, db, workflow, request, receipt, index, funding_id):
        if request.authorization_hash:
            raise DomainError("LEARNING_AUTHORIZATION_SEALED", "已固定授权不能替换。", 409)
        self.funding_check_in(db, workflow, request, funding_id)
        request.funding_id = funding_id
        request.authorization_receipt_id, request.authorization_action_index = receipt.id, index
        request.authorization_hash = fingerprint(
            {
                "request_id": request.id,
                "request_hash": request.request_hash,
                "funding_id": funding_id,
                "receipt_id": receipt.id,
                "action_index": index,
                "receipt_request_hash": receipt.request_hash,
            }
        )
        request.failure_code = None
        request.status = "ready" if request.execution_id else "pending_handoff"

    def record_in(
        self, db, conv, workflow, receipt, index, message, *, initial=False, explicit_question=None
    ):
        if initial:
            old = self.initial_in(db, workflow)
            if old:
                return old
        self.app.practice_workflows.require_no_active_in(db, conv.id)
        if self.pending_in(db, conv.id) or db.scalar(
            select(Turn.id).where(
                Turn.conversation_id == conv.id, Turn.status.in_(["queued", "running"])
            )
        ):
            raise DomainError("LEARNING_REQUEST_PENDING", "当前会话仍有未完成学习请求。", 409)
        selected_funding = message.authorization_ref or workflow.target.get("funding_ref")
        if selected_funding:
            self.app.dialogue._funding_in(
                db, workflow.principal_id, workflow.namespace_id, selected_funding
            )
            locked(db, FundingAgreement, selected_funding)
        require_assistance_in(db, workflow.principal_id)
        accepted = workflow.target.get("accepted_plan") or {}
        scope = accepted_binding_in(
            self.app,
            db,
            workflow.principal_id,
            conv,
            accepted.get("plan_id"),
            accepted.get("active_version_id"),
            accepted.get("revision"),
        )
        profile = profile_in(db, workflow.principal_id)
        accepted_binding_in(
            self.app,
            db,
            workflow.principal_id,
            conv,
            accepted.get("plan_id"),
            accepted.get("active_version_id"),
            accepted.get("revision"),
            module_id=profile.active_module_id,
        )
        if not profile.active_module_id:
            raise DomainError("LEARNING_MODULE_REQUIRED", "请明确选择学习模块。", 409)
        if (scope.get("goal") or {}).get("goal_version_id") != workflow.goal_version_id:
            raise DomainError("WORKFLOW_GOAL_SCOPE", "已采纳计划目标不属于当前工作流。", 409)
        options = getattr(message, "tutor_request", TutorRequestInputV1())
        parts = re.split(r"[，,]", message.text, maxsplit=1)
        suffix = parts[1].strip() if initial and len(parts) == 2 else None
        if suffix and options.question and suffix != options.question:
            raise DomainError("LEARNING_QUESTION_AMBIGUOUS", "明确问题与采纳消息问题不一致。", 422)
        question = options.question or explicit_question or suffix or DEFAULT_QUESTION
        previous = self.last_in(db, workflow)
        execution = self.app.session_workflows.current(db, workflow)
        payload = {
            "contract": "workflow-tutor-request-v1",
            "accepted": scope,
            "question": question,
            "model_call_cap": options.model_call_cap,
            "preferences": options.preferences.model_dump(mode="json"),
            "module_id": profile.active_module_id,
            "module_hash": scope["module_hashes"][profile.active_module_id],
            "profile_revision": profile.revision,
            "submitted_focus_revision": message.expected_focus_revision,
            "requested_funding_id": message.authorization_ref,
            "resolved_funding_id": message.authorization_ref or workflow.target.get("funding_ref"),
            "origin_request_hash": receipt.request_hash,
            "message_key_hash": fingerprint(message.idempotency_key),
            "model_profile": self.app.provider.profile_version,
        }
        request = WorkflowTutorRequest(
            workflow_id=workflow.id,
            conversation_id=conv.id,
            execution_id=execution.id
            if execution and execution.workflow_type == "learning_session"
            else None,
            predecessor_execution_id=execution.id
            if execution and execution.workflow_type == "knowledge_session"
            else None,
            action_receipt_id=receipt.id,
            action_index=index,
            sequence=previous.sequence + 1 if previous else 1,
            kind="initial" if initial else "followup",
            predecessor_request_id=previous.id if previous else None,
            predecessor_turn_id=previous.turn_id if previous else None,
            request=payload,
            request_hash=fingerprint(payload),
            status="pending_authorization",
        )
        db.add(request)
        db.flush()
        try:
            self.seal_in(db, workflow, request, receipt, index, payload["resolved_funding_id"])
        except DomainError as exc:
            if exc.code not in AUTH_ERRORS:
                raise
            request.failure_code = exc.code
            request.status = (
                "pending_handoff" if request.execution_id is None else "pending_authorization"
            )
        return request

    def authorize_in(self, db, conv, workflow, receipt, index, value):
        request = locked(db, WorkflowTutorRequest, value.request_id)
        if (
            not request
            or request.workflow_id != workflow.id
            or request.conversation_id != conv.id
            or request.request_hash != value.request_hash
        ):
            raise DomainError("NOT_FOUND", "学习请求不存在。", 404)
        receipt_in(db, request)
        if request.status not in {"pending_authorization", "pending_handoff"}:
            raise DomainError("LEARNING_REQUEST_STATE", "该请求不能追加授权。", 409)
        locked(db, FundingAgreement, value.authorization_ref)
        self.validate_scope_in(db, conv, workflow, request)
        self.seal_in(db, workflow, request, receipt, index, value.authorization_ref)
        return self.response(request)

    def validate_scope_in(self, db, conv, workflow, request):
        require_assistance_in(db, workflow.principal_id)
        if conv.focus.get("active_workflow_id") != workflow.id:
            raise DomainError("LEARNING_FOCUS_CHANGED", "请先明确切回学习工作流。", 409)
        original = request.request
        accepted = original["accepted"]
        current = accepted_binding_in(
            self.app,
            db,
            workflow.principal_id,
            conv,
            accepted["plan_id"],
            accepted["version_id"],
            accepted["plan_revision"],
            module_id=original["module_id"],
        )
        profile = profile_in(db, workflow.principal_id)
        if (
            profile.active_module_id != original["module_id"]
            or profile.revision != original["profile_revision"]
        ):
            raise DomainError(
                "LEARNING_SELECTION_CHANGED", "学习模块或档案已变化，请明确创建新请求。", 409
            )
        if current != accepted or original["model_profile"] != self.app.provider.profile_version:
            raise DomainError("LEARNING_BINDING_CHANGED", "学习请求的固定合同已变化。", 409)

    def validate_dispatch_in(self, db, actor, conv, request_id, session_value):
        request = db.get(WorkflowTutorRequest, request_id)
        if (
            not request
            or request.conversation_id != conv.id
            or request.status != "ready"
            or request.turn_id
            or request.run_id
        ):
            _fail()
        workflow, _ = _owner_in(db, request, actor, conv.namespace_id)
        if (
            request.materialized_input != session_value.model_dump(mode="json")
            or len(self.pending_in(db, conv.id)) != 1
        ):
            _fail()
        self.validate_scope_in(db, conv, workflow, request)
        return binding_for(request)

    def dispatch_in(self, db, conv, workflow, execution, request):
        self.validate_scope_in(db, conv, workflow, request)
        receipt_in(db, request)
        receipt_in(db, request, authorization=True)
        self.funding_check_in(db, workflow, request, request.funding_id)
        source = request.request
        accepted = source["accepted"]
        value = SessionTutorTurnInput(
            question=source["question"],
            expected_revision=conv.revision,
            idempotency_key="learning-request:" + request.id,
            funding_id=request.funding_id,
            model_call_cap=source["model_call_cap"],
            namespace_id=workflow.namespace_id,
            plan_id=accepted["plan_id"],
            version_id=accepted["version_id"],
            module_id=source["module_id"],
            expected_plan_revision=accepted["plan_revision"],
            preferences=source["preferences"],
        )
        request.materialized_input = value.model_dump(mode="json")
        request.materialized_hash = fingerprint(request.materialized_input)
        result = self.app.conversations._submit(
            workflow.principal_id,
            conv.id,
            value,
            db=db,
            session_value=value,
            learning_request_id=request.id,
        )
        request.turn_id, request.run_id, request.status = (
            result["turn_id"],
            result["run_id"],
            "dispatched",
        )
        self.app.workflows.link_in(db, workflow, db.get(AgentRun, request.run_id), "tutor")
        self.app.workflows.emit_in(
            db,
            conv,
            "workflow.tutor_started",
            "tutor-started:" + request.id,
            workflow,
            request_id=request.id,
            execution_id=execution.id,
            turn_id=request.turn_id,
            run_id=request.run_id,
        )

    def cancel_in(self, db, workflow):
        for request in self.pending_in(db, workflow.conversation_id):
            if request.workflow_id == workflow.id:
                request.status, request.failure_code = "cancelled", "WORKFLOW_CANCELLED"

    def lock_dispatch_funding_in(self, db, workflow):
        pending = [
            r for r in self.pending_in(db, workflow.conversation_id) if r.workflow_id == workflow.id
        ]
        if pending and pending[0].funding_id:
            locked(db, FundingAgreement, pending[0].funding_id)
