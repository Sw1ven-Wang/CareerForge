"""Receipt-owned practice effects. No model, credentials, or answers in graph state."""

import hashlib
from datetime import timedelta

from sqlalchemy import select

from ..db import aware, clock, locked
from ..errors import DomainError
from ..models import (
    AgentRun,
    FundingAgreement,
    Idempotency,
    Principal,
    Turn,
)
from ..models_dialogue import (
    ConversationActionReceipt,
    WorkflowAssessmentRequest,
    WorkflowExecution,
    WorkflowPracticeRequest,
    WorkflowRunLink,
    WorkflowSession,
)
from ..models_learning_ext import LearningAttempt
from ..util import fingerprint
from .dependencies import assert_source_scope, check_closure
from .learning_binding import accepted_binding_in, profile_in, validate_persisted_owner_in
from .learning_requests import AUTH_ERRORS
from .runtime import TERMINAL

GRAPH_VERSION = "learning-session-practice-v1"
ASSESSMENT_KEY = "learning_assessment_request"
PENDING_ASSESSMENT = {"pending_authorization", "ready", "dispatched", "blocked_reconciliation"}


def fail(message="练习请求、来源或固定内容已变化。"):
    raise DomainError("PRACTICE_BINDING", message, 409)


def outcome_in(db, identifier, index, workflow_id, allowed):
    receipt = db.get(ConversationActionReceipt, identifier)
    if not receipt or receipt.action_plan_hash != fingerprint(receipt.plan):
        fail()
    if receipt.plan.get("contract_version") != "conversation-action-plan-v4":
        fail()
    planned, outcomes = receipt.plan.get("actions", []), receipt.result.get("action_receipts", [])
    if type(index) is not int or index < 0 or index >= len(planned) or index >= len(outcomes):
        fail()
    result = outcomes[index]
    if (
        result.get("status") != "applied"
        or result.get("action_index") != index
        or result.get("workflow_id") != workflow_id
        or result.get("action_type") not in allowed
        or planned[index].get("action", {}).get("type") != result.get("action_type")
    ):
        fail()
    return receipt, result


def validate_practice_in(db, practice, *, access=True):
    if not practice or practice.request_hash != fingerprint(practice.request):
        fail()
    workflow = db.get(WorkflowSession, practice.workflow_id)
    execution = db.get(WorkflowExecution, practice.execution_id)
    if (
        not workflow
        or not execution
        or execution.graph_version != GRAPH_VERSION
        or workflow.current_execution_id != execution.id
        or execution.goal_binding_hash != fingerprint(execution.binding_payload)
        or workflow.learning_graph_version != GRAPH_VERSION
        or (practice.principal_id, practice.namespace_id, practice.conversation_id)
        != (workflow.principal_id, workflow.namespace_id, workflow.conversation_id)
        or execution.workflow_id != workflow.id
        or practice.request.get("execution_binding_hash") != execution.goal_binding_hash
        or practice.request.get("accepted") != execution.binding_payload.get("accepted")
    ):
        fail()
    validate_persisted_owner_in(db, workflow, execution, access=access)
    receipt, result = outcome_in(
        db, practice.action_receipt_id, practice.action_index, workflow.id, {"start_practice"}
    )
    if (
        receipt.conversation_id != practice.conversation_id
        or practice.request.get("origin_request_hash") != receipt.request_hash
        or result.get("practice_id") != practice.id
        or result.get("request_hash") != practice.request_hash
    ):
        fail()
    if practice.attempt_id:
        effect = db.scalar(
            select(Idempotency).where(
                Idempotency.principal_id == practice.principal_id,
                Idempotency.operation == "workflow.practice.create",
                Idempotency.key == practice.id,
            )
        )
        if (
            not effect
            or effect.request_hash != practice.request_hash
            or any(
                effect.result.get(key) != getattr(practice, key)
                for key in ("activity_id", "attempt_id", "specification_hash")
            )
            or effect.result.get("practice_id") != practice.id
            or effect.result.get("request_hash") != practice.request_hash
        ):
            fail("练习创建缺少原子领域效果回执。")
        attempt = db.get(LearningAttempt, practice.attempt_id)
        if (
            not attempt
            or attempt.principal_id != practice.principal_id
            or attempt.activity_id != practice.activity_id
            or attempt.workspace_id != practice.workspace_id
            or attempt.specification_hash != practice.specification_hash
        ):
            fail()
        if practice.submission:
            if attempt.state != "submitted" or attempt.submission_hash != practice.submission.get(
                "submission_hash"
            ):
                fail()
            if hashlib.sha256(
                attempt.response.encode("utf-8")
            ).hexdigest() != practice.submission.get("response_utf8_sha256"):
                fail()
            if practice.submission.get("receipt_id"):
                _, submitted = outcome_in(
                    db,
                    practice.submission["receipt_id"],
                    practice.submission["action_index"],
                    workflow.id,
                    {"submit_practice"},
                )
                if (
                    submitted.get("submission_hash") != attempt.submission_hash
                    or submitted.get("practice_id") != practice.id
                ):
                    fail()
            else:
                witness = db.get(Idempotency, practice.submission.get("domain_receipt_id"))
                if (
                    not witness
                    or witness.operation != "workflow.practice.submit"
                    or witness.key != practice.id
                    or witness.request_hash != attempt.submission_hash
                ):
                    fail()
    return workflow, execution


def assessment_binding(request, practice):
    return dict(
        contract="workflow-assessment-binding-v1",
        request_id=request.id,
        practice_id=practice.id,
        execution_id=practice.execution_id,
        workflow_id=practice.workflow_id,
        request_hash=request.request_hash,
        authorization_hash=request.authorization_hash,
    )


def validate_assessment_in(db, request, *, authorization=True):
    if (
        not request
        or request.request_hash != fingerprint(request.request)
        or request.request.get("authorization_expires_at")
        != aware(request.authorization_expires_at).isoformat()
    ):
        fail()
    practice = db.get(WorkflowPracticeRequest, request.practice_id)
    workflow, execution = validate_practice_in(db, practice)
    receipt, result = outcome_in(
        db, request.action_receipt_id, request.action_index, workflow.id, {"request_assessment"}
    )
    if (
        result.get("request_id") != request.id
        or result.get("request_hash") != request.request_hash
        or request.request.get("origin_request_hash") != receipt.request_hash
        or request.request.get("practice_hash") != practice.request_hash
        or request.request.get("submission_hash") != practice.submission.get("submission_hash")
    ):
        fail()
    if authorization:
        receipt, result = outcome_in(
            db,
            request.authorization_receipt_id,
            request.authorization_action_index,
            workflow.id,
            {"request_assessment", "authorize_assessment_request"},
        )
        if (
            result.get("request_id") != request.id
            or result.get("authorization_hash") != request.authorization_hash
            or result.get("funding_id") != request.funding_id
            or request.authorization_hash
            != fingerprint(
                dict(
                    request_id=request.id,
                    request_hash=request.request_hash,
                    funding_id=request.funding_id,
                    receipt_id=receipt.id,
                    action_index=request.authorization_action_index,
                    receipt_request_hash=receipt.request_hash,
                )
            )
        ):
            fail()
    return practice, workflow, execution


def validate_run_admission_in(db, actor, namespace_id, manifest):
    binding = manifest.get(ASSESSMENT_KEY)
    request = (
        db.get(WorkflowAssessmentRequest, binding.get("request_id"))
        if isinstance(binding, dict)
        else None
    )
    practice, workflow, _ = validate_assessment_in(db, request)
    if (
        binding != assessment_binding(request, practice)
        or practice.principal_id != actor
        or practice.namespace_id != namespace_id
        or request.status != "ready"
        or request.run_id
        or workflow.control_state != "active"
        or aware(request.authorization_expires_at) <= clock(db)
        or manifest.get("snapshot", {}).get("attempt_id") != practice.attempt_id
        or manifest.get("snapshot", {}).get("submission_hash")
        != practice.submission.get("submission_hash")
    ):
        fail()


def validate_run_in(db, run, *, allow_pause=False, history=False):
    binding = run.input_manifest.get(ASSESSMENT_KEY)
    linked = db.scalar(
        select(WorkflowAssessmentRequest).where(WorkflowAssessmentRequest.run_id == run.id)
    )
    if binding is None and linked is None and run.graph_version != "learning-assessment-v1":
        return
    if not isinstance(binding, dict) or not linked:
        fail()
    practice, workflow, _ = validate_assessment_in(db, linked)
    if (
        binding != assessment_binding(linked, practice)
        or linked.run_id != run.id
        or linked.funding_id != run.funding_id
        or run.principal_id != practice.principal_id
        or run.namespace_id != practice.namespace_id
        or run.workflow != "learning_assessment"
        or run.graph_version != "learning-assessment-v1"
        or run.workflow_engine != "langgraph"
        or run.input_manifest.get("snapshot", {}).get("attempt_id") != practice.attempt_id
        or (
            not history
            and workflow.control_state != "active"
            and not (
                allow_pause
                and workflow.control_state in {"pause_requested", "paused", "resume_requested"}
            )
        )
    ):
        fail()
    if not history:
        # lock_run already acquires Funding before Run. Re-read that exact grant
        # after lock waits; result persistence is a new use, not history reading.
        funding = locked(db, FundingAgreement, run.funding_id)
        if (
            not funding
            or funding.payer_id != run.principal_id
            or funding.namespace_id != run.namespace_id
        ):
            fail("评分资金归属已变化。")
        roots = run.input_manifest.get("access_dependencies", [])
        closure = check_closure(db, run.principal_id, roots)
        required = {
            "learning_state",
            *closure.source_data_classes,
            *run.input_manifest.get("data_classes", []),
        }
        if "evaluation" not in funding.allowed_roles or not required <= set(
            funding.allowed_data_classes
        ):
            raise DomainError("EGRESS_OR_ROLE_DENIED", "评价角色或数据类别授权已撤回。", 403)
        assert_source_scope(db, roots, funding.allowed_source_ids)


class WorkflowPractice:
    def __init__(self, app):
        self.app, self.db = app, app.db

    def active_in(self, db, conversation_id):
        return db.scalar(
            select(WorkflowPracticeRequest).where(
                WorkflowPracticeRequest.active_conversation_id == conversation_id
            )
        )

    def last_in(self, db, workflow):
        return db.scalar(
            select(WorkflowPracticeRequest)
            .where(WorkflowPracticeRequest.execution_id == workflow.current_execution_id)
            .order_by(WorkflowPracticeRequest.sequence.desc())
            .limit(1)
        )

    def assessment_in(self, db, practice):
        return db.scalar(
            select(WorkflowAssessmentRequest)
            .where(WorkflowAssessmentRequest.practice_id == practice.id)
            .order_by(WorkflowAssessmentRequest.sequence.desc())
            .limit(1)
        )

    def require_no_pending_assistance_in(self, db, actor):
        if db.scalar(
            select(WorkflowAssessmentRequest.id)
            .join(
                WorkflowPracticeRequest,
                WorkflowAssessmentRequest.practice_id == WorkflowPracticeRequest.id,
            )
            .where(
                WorkflowPracticeRequest.principal_id == actor,
                WorkflowAssessmentRequest.status.in_(PENDING_ASSESSMENT),
            )
        ):
            raise DomainError("ASSISTANCE_ACTIVE", "账户仍有待处理评分请求。", 409)

    def require_no_active_in(self, db, conversation_id):
        if self.active_in(db, conversation_id):
            raise DomainError(
                "PRACTICE_ACTIVE", "请先明确提交并结束或放弃当前练习；普通追问不会提交答案。", 409
            )

    def lock_dispatch_funding_in(self, db, workflow):
        practice = self.active_in(db, workflow.conversation_id)
        assessment = self.assessment_in(db, practice) if practice else None
        if assessment and assessment.funding_id and assessment.status in PENDING_ASSESSMENT:
            locked(db, FundingAgreement, assessment.funding_id)

    def current_in(self, db, conv, workflow, message):
        execution = locked(db, WorkflowExecution, workflow.current_execution_id)
        self.app.learning_workflows.validate_in(db, workflow, execution)
        if execution.graph_version != GRAPH_VERSION or execution.id != message.execution_id:
            fail("请明确准入支持练习的新学习执行；旧检查点不热迁移。")
        if (
            workflow.control_state != "active"
            or conv.focus.get("active_workflow_id") != workflow.id
        ):
            raise DomainError("WORKFLOW_NOT_ACTIVE", "请先恢复并选中原练习工作流。", 409)
        self.lock_dispatch_funding_in(db, workflow)
        accepted = execution.binding_payload["accepted"]
        accepted_binding_in(
            self.app,
            db,
            workflow.principal_id,
            conv,
            accepted["plan_id"],
            accepted["version_id"],
            accepted["plan_revision"],
        )
        profile = profile_in(db, workflow.principal_id)
        return execution, profile

    @staticmethod
    def response(practice):
        return dict(
            practice_id=practice.id,
            request_id=practice.id,
            request_hash=practice.request_hash,
            execution_id=practice.execution_id,
            practice_status=practice.status,
            attempt_id=practice.attempt_id,
            activity_id=practice.activity_id,
            specification_hash=practice.specification_hash,
            competence_verified=False,
        )

    def command_in(self, db, actor, conv, workflow, message, receipt, index):
        from ..contracts_practice_dialogue_v1 import PracticeDialogueMessageV1

        if not isinstance(message, PracticeDialogueMessageV1):
            raise DomainError("PRACTICE_CALLER_CONTRACT", "需要明确的本人练习输入。", 422)
        if message.intent.type == "acknowledge_module":
            from .session_tutor import _funding_receipt_in

            witness = _funding_receipt_in(db, actor, message.intent.acknowledgment.turn_id)
            funding_id = witness.result.get("authority", {}).get("funding_id") if witness else None
            if funding_id:
                locked(db, FundingAgreement, funding_id)
        execution, profile = self.current_in(db, conv, workflow, message)
        value, name = message.intent, message.intent.type
        if name == "start_practice":
            self.require_no_active_in(db, conv.id)
            if self.app.learning_requests.pending_in(db, conv.id) or db.scalar(
                select(Turn.id).where(
                    Turn.conversation_id == conv.id, Turn.status.in_(["queued", "running"])
                )
            ):
                raise DomainError("LEARNING_REQUEST_PENDING", "先完成原导师请求。", 409)
            accepted = execution.binding_payload["accepted"]
            if (
                profile.revision != value.expected_profile_revision
                or profile.active_module_id != value.activity.module_id
                or accepted["module_hashes"].get(value.activity.module_id) != value.module_hash
            ):
                raise DomainError("REVISION_CONFLICT", "练习模块或档案已变化。", 409)
            activity = value.activity.model_copy(update={"explicitly_requested": True})
            previous = self.last_in(db, workflow)
            tutor = self.app.learning_requests.last_in(db, workflow)
            source = None
            if (
                tutor
                and tutor.status == "completed"
                and tutor.request["module_id"] == value.activity.module_id
            ):
                from .learning_requests import receipt_in, validate_delivered_request_in

                receipt_in(db, tutor)
                run = db.get(AgentRun, tutor.run_id)
                validate_delivered_request_in(
                    db, run, run.input_manifest.get("learning_tutor_request")
                )
                source = dict(request_id=tutor.id, turn_id=tutor.turn_id, run_id=tutor.run_id)
            payload = dict(
                contract="workflow-practice-binding-v1",
                execution_binding_hash=execution.goal_binding_hash,
                accepted=accepted,
                module_id=value.activity.module_id,
                module_hash=value.module_hash,
                profile_revision=profile.revision,
                activity=activity.model_dump(mode="json"),
                assistance_mode=value.assistance_mode,
                source_tutor=source,
                origin_request_hash=receipt.request_hash,
            )
            practice = WorkflowPracticeRequest(
                principal_id=actor,
                namespace_id=conv.namespace_id,
                conversation_id=conv.id,
                active_conversation_id=conv.id,
                workflow_id=workflow.id,
                execution_id=execution.id,
                action_receipt_id=receipt.id,
                action_index=index,
                sequence=previous.sequence + 1 if previous else 1,
                request=payload,
                request_hash=fingerprint(payload),
                status="queued",
            )
            db.add(practice)
            db.flush()
            return self.response(practice)
        if name == "authorize_assessment_request":
            assessment = db.get(WorkflowAssessmentRequest, value.assessment_request_id)
            owned = db.get(WorkflowPracticeRequest, assessment.practice_id) if assessment else None
            if not owned or owned.principal_id != actor or owned.conversation_id != conv.id:
                raise DomainError("NOT_FOUND", "评分请求不存在。", 404)
            practice, owner, _ = validate_assessment_in(db, assessment, authorization=False)
            if (
                owner.id != workflow.id
                or assessment.request_hash != value.request_hash
                or assessment.status != "pending_authorization"
            ):
                fail()
            if (
                profile.active_module_id != practice.request["module_id"]
                or profile.revision != practice.request["profile_revision"]
            ):
                raise DomainError("REVISION_CONFLICT", "原练习的模块选择已变化。", 409)
            self.seal_in(db, practice, assessment, receipt, index, message.authorization_ref)
            return self.assessment_response(assessment, practice)
        practice = db.get(WorkflowPracticeRequest, value.practice_id)
        if not practice or practice.principal_id != actor or practice.conversation_id != conv.id:
            raise DomainError("NOT_FOUND", "练习不存在。", 404)
        owner, _ = validate_practice_in(db, practice)
        if (
            owner.id != workflow.id
            or practice.request_hash != value.practice_hash
            or practice.attempt_id != value.attempt_id
            or practice.specification_hash != value.specification_hash
            or practice.active_conversation_id != conv.id
        ):
            fail()
        if (
            profile.active_module_id != practice.request["module_id"]
            or profile.revision != practice.request["profile_revision"]
        ):
            raise DomainError("REVISION_CONFLICT", "练习的模块选择已变化。", 409)
        if name == "submit_practice":
            if not value.submission.response.strip():
                raise DomainError("PRACTICE_ANSWER_REQUIRED", "请明确提交非空原始答案。", 422)
            if practice.submission:
                raise DomainError(
                    "ATTEMPT_IMMUTABLE", "该练习已有提交命令；同键重放或创建新练习。", 409
                )
            self.app.learning.submit_attempt_in(db, actor, practice.attempt_id, value.submission)
            attempt = db.get(LearningAttempt, practice.attempt_id)
            practice.submission = self.submission_witness(
                attempt, receipt_id=receipt.id, action_index=index
            )
            practice.status = "submitted"
            return {**self.response(practice), "submission_hash": attempt.submission_hash}
        if name == "request_assessment":
            if (
                practice.status != "submitted"
                or not practice.submission
                or value.submission_hash != practice.submission["submission_hash"]
            ):
                fail("评分需要确切的已冻结本人提交。")
            previous = self.assessment_in(db, practice)
            if previous:
                if previous.status in PENDING_ASSESSMENT:
                    raise DomainError("ASSESSMENT_PENDING", "请处理原评分请求。", 409)
                if previous.run_id:
                    from .run_pause import _uncertain

                    run = db.get(AgentRun, previous.run_id)
                    if run.status not in TERMINAL or _uncertain(db, run):
                        raise DomainError("ASSESSMENT_RECONCILIATION", "原调用结果尚未对账。", 409)
            authorization_expires_at = clock(db) + timedelta(hours=24)
            payload = dict(
                authorization_expires_at=authorization_expires_at.isoformat(),
                contract="workflow-assessment-request-v1",
                practice_hash=practice.request_hash,
                attempt_id=practice.attempt_id,
                specification_hash=practice.specification_hash,
                submission_hash=value.submission_hash,
                requested_funding_id=message.authorization_ref,
                predecessor_request_id=previous.id if previous else None,
                origin_request_hash=receipt.request_hash,
            )
            assessment = WorkflowAssessmentRequest(
                practice_id=practice.id,
                active_practice_id=practice.id,
                action_receipt_id=receipt.id,
                action_index=index,
                sequence=previous.sequence + 1 if previous else 1,
                request=payload,
                request_hash=fingerprint(payload),
                status="pending_authorization",
                authorization_expires_at=authorization_expires_at,
            )
            db.add(assessment)
            db.flush()
            try:
                self.seal_in(db, practice, assessment, receipt, index, message.authorization_ref)
            except DomainError as exc:
                if exc.code not in AUTH_ERRORS:
                    raise
                assessment.failure_code = exc.code
            return self.assessment_response(assessment, practice)
        if name == "record_practice_observation":
            return {
                **self.response(practice),
                **self.app.learning.record_observation(
                    actor, practice.attempt_id, value.observation, db=db
                ),
            }
        if name == "record_tutor_feedback":
            source = practice.request.get("source_tutor")
            if not source or value.feedback.turn_id != source["turn_id"]:
                fail("反馈必须指向本练习真实已完成的导师回答。")
            turn = db.get(Turn, source["turn_id"])
            if not turn or turn.status != "completed" or turn.conversation_id != conv.id:
                fail()
            return {
                **self.response(practice),
                **self.app.evolution.feedback(actor, value.feedback, db=db),
            }
        if name == "acknowledge_module":
            from .session_progress import acknowledge_module_in

            source = practice.request.get("source_tutor")
            ack = value.acknowledgment
            if (
                not source
                or ack.turn_id != source["turn_id"]
                or ack.attempt_id != practice.attempt_id
                or ack.module_id != practice.request["module_id"]
            ):
                fail("确认需要该模块真实教学交付与同一提交。")
            self.require_settled_in(db, practice)
            result = acknowledge_module_in(self.app, db, actor, ack, practice=practice)
            practice.status, practice.active_conversation_id = "completed", None
            return {**self.response(practice), **result}
        if name in {"end_practice", "abandon_practice"}:
            self.require_settled_in(db, practice)
            if name == "end_practice" and practice.status != "submitted":
                raise DomainError("ATTEMPT_NOT_SUBMITTED", "未提交练习请明确放弃。", 422)
            self.app.learning.close_attempt_in(db, actor, practice.attempt_id)
            practice.status = "completed" if practice.submission else "abandoned"
            practice.active_conversation_id = None
            return self.response(practice)
        fail()

    @staticmethod
    def submission_witness(attempt, **origin):
        return dict(
            submission_hash=attempt.submission_hash,
            response_utf8_sha256=hashlib.sha256(attempt.response.encode("utf-8")).hexdigest(),
            **origin,
        )

    def require_settled_in(self, db, practice):
        request = self.assessment_in(db, practice)
        if request and request.status in PENDING_ASSESSMENT:
            raise DomainError("ASSESSMENT_PENDING", "请先等待、授权或取消原评分请求。", 409)
        if request and request.run_id:
            from .run_pause import _uncertain

            run = db.get(AgentRun, request.run_id)
            if run.status not in TERMINAL or _uncertain(db, run):
                raise DomainError("ASSESSMENT_RECONCILIATION", "原评分结果未安全结算。", 409)

    def funding_check_in(self, db, practice, funding_id):
        funding = locked(db, FundingAgreement, funding_id) if funding_id else None
        if not funding:
            raise DomainError(
                "NOT_FOUND" if funding_id else "FUNDING_UNAVAILABLE", "评分需要明确费用授权。", 403
            )
        if (
            funding.payer_id != practice.principal_id
            or funding.namespace_id != practice.namespace_id
        ):
            raise DomainError("NOT_FOUND", "费用授权不可访问。", 404)
        if funding.status != "active" or aware(funding.expires_at) <= clock(db):
            raise DomainError("FUNDING_UNAVAILABLE", "费用授权已失效。", 403)
        if funding.calls_consumed + funding.calls_held + 1 > funding.call_cap:
            raise DomainError("BUDGET_EXHAUSTED", "评分需要一个独立调用额度。", 409)
        closure = check_closure(db, practice.principal_id, [practice.activity_id])
        if "evaluation" not in funding.allowed_roles or not {
            "learning_state",
            *closure.source_data_classes,
        } <= set(funding.allowed_data_classes):
            raise DomainError("EGRESS_OR_ROLE_DENIED", "评价角色或来源类别未授权。", 403)
        assert_source_scope(db, [practice.activity_id], funding.allowed_source_ids)
        return funding

    def seal_in(self, db, practice, request, receipt, index, funding_id):
        if request.authorization_hash or aware(request.authorization_expires_at) <= clock(db):
            raise DomainError("ASSESSMENT_AUTHORIZATION", "原评分授权已固定或过期。", 409)
        self.funding_check_in(db, practice, funding_id)
        request.funding_id = funding_id
        request.authorization_receipt_id, request.authorization_action_index = receipt.id, index
        request.authorization_hash = fingerprint(
            dict(
                request_id=request.id,
                request_hash=request.request_hash,
                funding_id=funding_id,
                receipt_id=receipt.id,
                action_index=index,
                receipt_request_hash=receipt.request_hash,
            )
        )
        request.status, request.failure_code = "ready", None

    @staticmethod
    def assessment_response(request, practice):
        return dict(
            practice_id=practice.id,
            request_id=request.id,
            request_hash=request.request_hash,
            assessment_request_id=request.id,
            assessment_status=request.status,
            funding_id=request.funding_id,
            authorization_hash=request.authorization_hash,
            authorization_expires_at=request.authorization_expires_at.isoformat(),
            competence_verified=False,
        )

    def route_in(self, db, workflow, execution):
        practice = self.active_in(db, workflow.conversation_id)
        if not practice or practice.workflow_id != workflow.id:
            return None
        validate_practice_in(db, practice)
        if practice.status == "queued":
            return "create_practice"
        if practice.status == "waiting_answer":
            return "wait_practice_answer"
        if practice.status != "submitted":
            fail()
        request = self.assessment_in(db, practice)
        if not request:
            return "wait_practice_feedback"
        validate_assessment_in(db, request, authorization=bool(request.authorization_hash))
        if request.status == "pending_authorization":
            return (
                "expire_assessment"
                if aware(request.authorization_expires_at) <= clock(db)
                else "wait_assessment_authorization"
            )
        if request.status == "ready":
            return "dispatch_assessment"
        if request.status in {"dispatched", "blocked_reconciliation"}:
            from .run_pause import _uncertain

            run = db.get(AgentRun, request.run_id)
            if not run:
                fail()
            if _uncertain(db, run):
                return "wait_practice_reconciliation"
            return "reconcile_assessment" if run.status in TERMINAL else "wait_assessment_run"
        return "wait_practice_feedback"

    def operate_in(self, db, conv, workflow, execution, stage):
        practice = self.active_in(db, conv.id)
        validate_practice_in(db, practice)
        if stage == "create_practice":
            from ..contracts_learning_ext import ActivityInput, AttemptInput

            effect = db.scalar(
                select(Idempotency).where(
                    Idempotency.principal_id == practice.principal_id,
                    Idempotency.operation == "workflow.practice.create",
                    Idempotency.key == practice.id,
                )
            )
            if effect:
                if (
                    effect.request_hash != practice.request_hash
                    or effect.result.get("attempt_id") != practice.attempt_id
                ):
                    fail()
                fail("练习创建效果已提交但请求状态不一致，需要对账。")
            if self.app.learning_requests.pending_in(db, conv.id):
                raise DomainError("LEARNING_REQUEST_PENDING", "原导师请求尚未完成。", 409)
            accepted = practice.request["accepted"]
            accepted_binding_in(
                self.app,
                db,
                practice.principal_id,
                conv,
                accepted["plan_id"],
                accepted["version_id"],
                accepted["plan_revision"],
                module_id=practice.request["module_id"],
            )
            profile = profile_in(db, practice.principal_id)
            if (
                profile.active_module_id != practice.request["module_id"]
                or profile.revision != practice.request["profile_revision"]
            ):
                raise DomainError("REVISION_CONFLICT", "练习创建前模块已变化。", 409)
            activity = self.app.learning.create_activity(
                practice.principal_id,
                ActivityInput.model_validate(practice.request["activity"]),
                db=db,
            )
            activity_id = activity.get("activity_id")
            attempt = self.app.learning.start_attempt(
                practice.principal_id,
                activity_id,
                AttemptInput(
                    assistance_mode=practice.request["assistance_mode"],
                    idempotency_key="practice:" + practice.id,
                ),
                db=db,
            )
            row = db.get(LearningAttempt, attempt["attempt_id"])
            practice.activity_id, practice.attempt_id, practice.workspace_id = (
                activity_id,
                row.id,
                row.workspace_id,
            )
            practice.specification_hash, practice.status = row.specification_hash, "waiting_answer"
            db.add(
                Idempotency(
                    principal_id=practice.principal_id,
                    operation="workflow.practice.create",
                    key=practice.id,
                    request_hash=practice.request_hash,
                    result=self.response(practice),
                )
            )
            self.emit_in(db, conv, workflow, practice, "created")
            return
        request = self.assessment_in(db, practice)
        validate_assessment_in(db, request, authorization=bool(request.authorization_hash))
        if stage == "expire_assessment":
            request.status, request.active_practice_id = "expired", None
        elif stage == "dispatch_assessment":
            from ..contracts_assessment_v166 import AssessmentInput

            try:
                with db.begin_nested():
                    if aware(request.authorization_expires_at) <= clock(db):
                        raise DomainError("ASSESSMENT_AUTHORIZATION", "原授权请求已过期。", 409)
                    accepted = practice.request["accepted"]
                    accepted_binding_in(
                        self.app,
                        db,
                        practice.principal_id,
                        conv,
                        accepted["plan_id"],
                        accepted["version_id"],
                        accepted["plan_revision"],
                        module_id=practice.request["module_id"],
                    )
                    profile = profile_in(db, practice.principal_id)
                    if (
                        profile.active_module_id != practice.request["module_id"]
                        or profile.revision != practice.request["profile_revision"]
                    ):
                        raise DomainError("REVISION_CONFLICT", "评分派发前练习选择已变化。", 409)
                    self.funding_check_in(db, practice, request.funding_id)
                    result = self.app.assessments.start_in(
                        db,
                        practice.principal_id,
                        AssessmentInput(
                            namespace_id=practice.namespace_id,
                            funding_id=request.funding_id,
                            attempt_id=practice.attempt_id,
                            idempotency_key="assessment:" + request.id,
                        ),
                        practice_binding=assessment_binding(request, practice),
                    )
                    request.run_id, request.status = result["run_id"], "dispatched"
                    db.add(
                        WorkflowRunLink(
                            workflow_id=workflow.id,
                            run_id=request.run_id,
                            milestone="assessment",
                            ordinal=practice.sequence * 100000 + request.sequence,
                            goal_version_id=workflow.goal_version_id,
                        )
                    )
            except DomainError as exc:
                request.status, request.failure_code, request.active_practice_id = (
                    "rejected",
                    exc.code,
                    None,
                )
        elif stage == "reconcile_assessment":
            run = db.get(AgentRun, request.run_id)
            if run.status == "succeeded":
                result = self.app.runtime.stage(run.id, "result")
                self.app.assessments.validate_result(db, practice.principal_id, run, result)
                request.result_hash, request.status = fingerprint(result), "completed"
            else:
                request.status, request.failure_code = (
                    "rejected",
                    "ASSESSMENT_" + run.status.upper(),
                )
            request.active_practice_id = None
        else:
            fail()
        self.emit_in(
            db,
            conv,
            workflow,
            practice,
            stage,
            assessment_request_id=request.id,
            run_id=request.run_id,
        )

    def emit_in(self, db, conv, workflow, practice, kind, **refs):
        self.app.workflows.emit_in(
            db,
            conv,
            "workflow.practice_" + kind,
            "practice:"
            + practice.id
            + ":"
            + kind
            + ":"
            + str(refs.get("assessment_request_id", "")),
            workflow,
            practice_id=practice.id,
            attempt_id=practice.attempt_id,
            **refs,
        )

    def guard_direct_ack_in(self, db, actor, value, *, practice=None):
        # Called only after the original ACK's Funding/Plan/Profile locks and
        # receipt replay. Never acquire Principal/Conversation/Workflow here.
        # The optional object comes from command_in, never from the HTTP caller.
        matched = False
        for active in db.scalars(
            select(WorkflowPracticeRequest)
            .where(
                WorkflowPracticeRequest.principal_id == actor,
                WorkflowPracticeRequest.active_conversation_id.is_not(None),
            )
            .execution_options(populate_existing=True)
        ):
            source = active.request.get("source_tutor") or {}
            accepted = active.request["accepted"]
            affected = source.get("turn_id") == value.turn_id or (
                accepted["plan_id"] == value.plan_id
                and accepted["version_id"] == value.version_id
                and active.request["module_id"] == value.module_id
            )
            if not affected:
                continue
            if practice is None or practice is not active:
                raise DomainError(
                    "PRACTICE_ACK_REQUIRED", "请在原会话明确确认本练习，不能绕过原控制。", 409
                )
            workflow, execution = validate_practice_in(db, active)
            if (
                active.principal_id != actor
                or active.namespace_id != value.namespace_id
                or source.get("turn_id") != value.turn_id
                or active.attempt_id != value.attempt_id
                or accepted["plan_id"] != value.plan_id
                or accepted["version_id"] != value.version_id
                or active.request["module_id"] != value.module_id
                or workflow.control_state != "active"
                or workflow.current_execution_id != execution.id
                or active.status != "submitted"
            ):
                fail("模块确认的内部练习来源不一致。")
            matched = True
        if practice is not None and not matched:
            fail("模块确认缺少当前内部练习。")

    def guard_direct_assessment_in(self, db, actor, value):
        locked(db, Principal, actor)
        practice = db.scalar(
            select(WorkflowPracticeRequest).where(
                WorkflowPracticeRequest.attempt_id == value.attempt_id
            )
        )
        if practice:
            raise DomainError(
                "PRACTICE_ASSESSMENT_REQUIRED",
                "请通过原会话明确请求评分，不能绕过原授权与控制。",
                409,
            )

    def guard_direct_attempt_in(self, db, actor, identifier, operation):
        locked(db, Principal, actor)
        practice = db.scalar(
            select(WorkflowPracticeRequest).where(WorkflowPracticeRequest.attempt_id == identifier)
        )
        if not practice:
            return
        conv = self.app.workflows.conversation(db, actor, practice.conversation_id, lock=True)
        workflow = locked(db, WorkflowSession, practice.workflow_id)
        locked(db, WorkflowExecution, practice.execution_id)
        self.lock_dispatch_funding_in(db, workflow)
        validate_practice_in(db, practice)
        if (
            practice.principal_id != actor
            or workflow.control_state != "active"
            or not practice.active_conversation_id
        ):
            raise DomainError("WORKFLOW_NOT_ACTIVE", "请在原练习工作流内操作。", 409)
        accepted = practice.request["accepted"]
        accepted_binding_in(
            self.app,
            db,
            actor,
            conv,
            accepted["plan_id"],
            accepted["version_id"],
            accepted["plan_revision"],
        )
        profile = profile_in(db, actor)
        if (
            profile.active_module_id != practice.request["module_id"]
            or profile.revision != practice.request["profile_revision"]
        ):
            raise DomainError("REVISION_CONFLICT", "原练习的模块选择已变化。", 409)
        if operation in {"close", "abandon"}:
            self.require_settled_in(db, practice)

    def after_direct_attempt_in(self, db, actor, identifier, operation):
        practice = db.scalar(
            select(WorkflowPracticeRequest).where(WorkflowPracticeRequest.attempt_id == identifier)
        )
        if not practice:
            return
        attempt = db.get(LearningAttempt, identifier)
        if attempt.state == "submitted" and not practice.submission:
            witness = Idempotency(
                principal_id=actor,
                operation="workflow.practice.submit",
                key=practice.id,
                request_hash=attempt.submission_hash,
                result={"attempt_id": identifier},
            )
            db.add(witness)
            db.flush()
            practice.submission = self.submission_witness(attempt, domain_receipt_id=witness.id)
            practice.status = "submitted"
        if operation in {"close", "abandon"}:
            practice.status = "completed" if practice.submission else "abandoned"
            practice.active_conversation_id = None
        workflow, execution = validate_practice_in(db, practice)
        self.app.learning_workflows.wake_in(db, workflow, execution, "practice_direct")

    def cancel_in(self, db, workflow):
        practice = self.active_in(db, workflow.conversation_id)
        if not practice or practice.workflow_id != workflow.id:
            return
        if practice.attempt_id:
            self.app.learning.close_attempt_in(db, practice.principal_id, practice.attempt_id)
        for request in db.scalars(
            select(WorkflowAssessmentRequest).where(
                WorkflowAssessmentRequest.practice_id == practice.id,
                WorkflowAssessmentRequest.status.in_(PENDING_ASSESSMENT),
            )
        ):
            request.status, request.active_practice_id = "cancelled", None
        practice.status, practice.active_conversation_id = "cancelled", None

    def reap_expired(self):
        with self.db.read() as db:
            identities = list(
                db.scalars(
                    select(WorkflowAssessmentRequest.id).where(
                        WorkflowAssessmentRequest.status == "pending_authorization",
                        WorkflowAssessmentRequest.authorization_expires_at <= clock(db),
                    )
                )
            )
        for identifier in identities:
            with self.db.transaction() as db:
                request = db.get(WorkflowAssessmentRequest, identifier)
                practice = db.get(WorkflowPracticeRequest, request.practice_id)
                locked(db, Principal, practice.principal_id)
                self.app.workflows.conversation(
                    db, practice.principal_id, practice.conversation_id, lock=True
                )
                workflow = locked(db, WorkflowSession, practice.workflow_id)
                execution = locked(db, WorkflowExecution, practice.execution_id)
                request = locked(db, WorkflowAssessmentRequest, identifier)
                if (
                    request.status == "pending_authorization"
                    and workflow.control_state == "active"
                    and execution.status == "waiting"
                ):
                    self.app.learning_workflows.wake_in(
                        db, workflow, execution, "assessment_expiry"
                    )

    def detail(self, actor, conversation_id, identifier):
        with self.db.read() as db:
            self.app.workflows.conversation(db, actor, conversation_id)
            practice = db.get(WorkflowPracticeRequest, identifier)
            if (
                not practice
                or practice.conversation_id != conversation_id
                or practice.principal_id != actor
            ):
                raise DomainError("NOT_FOUND", "练习不存在。", 404)
            validate_practice_in(db, practice)
            result = self.response(practice)
            if practice.activity_id:
                result["activity"] = self.app.learning._activity(
                    db, actor, practice.activity_id
                ).specification
            result["submission_hash"] = practice.submission.get("submission_hash")
            request = self.assessment_in(db, practice)
            if request:
                validate_assessment_in(db, request, authorization=bool(request.authorization_hash))
                result["assessment"] = self.assessment_response(request, practice)
                if request.status == "completed":
                    run = db.get(AgentRun, request.run_id)
                    candidate = self.app.runtime.stage(run.id, "result")
                    self.app.assessments.validate_result(db, actor, run, candidate)
                    if fingerprint(candidate) != request.result_hash:
                        fail()
                    result["assessment_candidate"] = candidate
            return result
