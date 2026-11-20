"""Durable teaching sessions and explicit user-acknowledged module progression.

A user ACK records the user's choice, not an academic/clinical competence verdict.
Reading never creates an exercise. Model completion never calls this service.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy

from sqlalchemy import select

from ..contracts_answer_completion_v166 import ANSWER_COMPLETION_STAGE, EVIDENCE_ANSWER_STATE
from ..contracts_session_progress_v166 import SessionModuleAckInput
from ..db import locked
from ..errors import DomainError
from ..models import (
    AgentRun,
    AnswerBlock,
    Idempotency,
    LearningPlan,
    PlanVersion,
    StageArtifact,
    StreamEvent,
    Turn,
    Workspace,
)
from ..models_learning_ext import LearningAttempt, LearningProfile
from ..util import fingerprint
from .bounded_evidence import refresh_context_pack
from .dependencies import check_closure, depend, register, require_object
from .privacy import assert_personal_access
from .security import private_namespace
from .session_tutor import SESSION_TUTOR_KEY, validate_session_tutor_in

SESSION_ACTIVITY_CONTRACT = "bound-reading-teaching-session-1.6.6-v1"
MODULE_ACK_CONTRACT = "explicit-session-module-ack-1.6.6-v1"


def create_teaching_session_in(db, actor, snapshot, token):
    payload = {
        "contract": SESSION_ACTIVITY_CONTRACT,
        "principal_id": actor,
        "namespace_id": snapshot["namespace_id"],
        "turn_id": snapshot["turn_id"],
        "conversation_id": snapshot["conversation_id"],
        "privacy_generation": snapshot["privacy_generation"],
        "session_tutor_binding": token,
        "module": snapshot["module"],
        "activity_kind": "reading_and_tutoring",
        "completion_authority": "explicit_authenticated_owner_ack",
        "exercise_created": False,
        "competence_verified": False,
    }
    session = register(
        db,
        actor,
        snapshot["namespace_id"],
        "learning_session",
        payload,
        snapshot["learning_session_id"],
    )
    depend(db, actor, session.id, token["manifest_id"], "uses_session_selection")
    return session


def _session_in(db, actor, session_id, *, history=True):
    ns = private_namespace(db, actor)
    obj = require_object(db, actor, session_id, "learning_session")
    payload = obj.payload
    turn = db.get(Turn, payload.get("turn_id"))
    if (
        obj.namespace_id != ns.id
        or payload.get("principal_id") != actor
        or payload.get("contract") != SESSION_ACTIVITY_CONTRACT
        or turn is None
    ):
        raise DomainError("NOT_FOUND", "教学活动会话不可访问。", 404)
    active = db.get(AgentRun, turn.run_id) if turn.run_id else None
    snapshot = validate_session_tutor_in(db, actor, turn, run=active, history=history)
    if (
        snapshot is None
        or snapshot.get("learning_session_id") != obj.id
        or payload.get("session_tutor_binding") != turn.focus[SESSION_TUTOR_KEY]
        or payload.get("module") != snapshot["module"]
    ):
        raise DomainError("SESSION_ACTIVITY_BINDING", "教学会话与固定导师模块不一致。", 409)
    check_closure(db, actor, [obj.id])
    return obj, turn, snapshot


def _delivery_in(app, db, actor, turn, snapshot):
    if turn.status != "completed":
        raise DomainError("SESSION_DELIVERY_PENDING", "先完成本次教学内容交付，再由本人确认。", 409)
    token = turn.focus[SESSION_TUTOR_KEY]
    if turn.run_id:
        run = db.get(AgentRun, turn.run_id)
        stage = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == turn.run_id,
                StageArtifact.stage_key == ANSWER_COMPLETION_STAGE,
            )
        )
        if (
            not run
            or run.status not in {"succeeded", "partially_succeeded"}
            or not stage
            or stage.input_hash != run.input_hash
        ):
            raise DomainError("SESSION_DELIVERY_PENDING", "教学运行尚未形成有效终态。", 409)
        assessment = stage.payload
    else:
        assessment = turn.focus.get(EVIDENCE_ANSWER_STATE, {}).get("assessment")
    terminal = db.scalar(
        select(StreamEvent).where(
            StreamEvent.turn_id == turn.id, StreamEvent.event_type == "answer.completed"
        )
    )
    if (
        not isinstance(assessment, dict)
        or not terminal
        or assessment.get("assessment_hash")
        != fingerprint({k: v for k, v in assessment.items() if k != "assessment_hash"})
        or assessment.get("binding_hash") != fingerprint(assessment.get("binding"))
        or assessment.get("binding", {}).get(SESSION_TUTOR_KEY) != token
        or assessment["binding"].get("turn_id") != turn.id
        or assessment["binding"].get("retrieval_manifest_id") != token["manifest_id"]
        or assessment["binding"].get("question_sha256")
        != hashlib.sha256(turn.question.encode()).hexdigest()
        or terminal.payload.get("answer_completion_assessment_hash")
        != assessment["assessment_hash"]
    ):
        raise DomainError("SESSION_DELIVERY_BINDING", "交付和原问题完整性评估缺少一致绑定。", 409)
    rows = list(
        db.scalars(
            select(AnswerBlock).where(AnswerBlock.turn_id == turn.id).order_by(AnswerBlock.ordinal)
        )
    )
    if not rows or [r.ordinal for r in rows] != list(range(len(rows))):
        raise DomainError("SESSION_DELIVERY_BINDING", "交付块不完整。", 409)
    check_closure(db, actor, [r.id for r in rows])
    pack = refresh_context_pack(
        app.knowledge, db, actor, snapshot["module"]["publication_id"], token["manifest_id"]
    )
    available = {f["fact_atom_id"] for f in pack["facts"]}
    delivered, base = set(), []
    for row in rows:
        obj = require_object(db, actor, row.id, "answer_block")
        if (
            obj.content_hash != row.content_hash
            or obj.payload != row.content
            or fingerprint(row.content) != row.content_hash
        ):
            raise DomainError("SESSION_DELIVERY_BINDING", "交付块内容已经改变。", 409)
        if row.content.get("answer_completion_contract") is None:
            base.append({"id": row.id, "content_hash": row.content_hash})
        if row.kind in {"repository_fact", "concept_fact"}:
            ids = row.content.get("fact_atom_ids", [])
            if (
                len(ids) != 1
                or not set(ids) <= available
                or row.content.get("publication_ids") != [pack["publication_id"]]
            ):
                raise DomainError("SESSION_DELIVERY_BINDING", "交付事实超出本模块。", 409)
            delivered.update(ids)
    if assessment["binding"].get("blocks") != base:
        raise DomainError("SESSION_DELIVERY_BINDING", "终态评估没有绑定实际最终交付。", 409)
    body = {
        "turn_id": turn.id,
        "session_tutor_binding": deepcopy(token),
        "blocks": [
            {"block_id": r.id, "ordinal": r.ordinal, "content_hash": r.content_hash} for r in rows
        ],
        "completion_assessment_hash": assessment["assessment_hash"],
        "model_call_usage": {
            "authorized_cap": run.call_cap if turn.run_id else 0,
            "consumed": run.calls_consumed if turn.run_id else 0,
            "held": run.held_calls if turn.run_id else 0,
        },
        "answer_coverage_status": assessment["status"],
        "task_satisfied": assessment["task_satisfied"],
        "uncovered_questions": assessment["uncovered_questions"],
        "delivered_fact_ids": sorted(delivered),
        "undelivered_module_fact_ids": sorted(available - delivered),
        "has_evidence_gap": any(r.kind == "evidence_gap" for r in rows),
        "delivery_complete": True,
        "competence_verified": False,
    }
    body["requires_incomplete_acceptance"] = bool(
        body["answer_coverage_status"] != "complete"
        or body["undelivered_module_fact_ids"]
        or body["has_evidence_gap"]
    )
    return {**body, "delivery_hash": fingerprint(body)}


def _attempt_in(app, db, actor, module, attempt_id):
    if attempt_id is None:
        return None
    attempt = db.scalar(
        select(LearningAttempt).where(LearningAttempt.id == attempt_id).with_for_update(read=True)
    )
    if not attempt or attempt.principal_id != actor or attempt.state != "submitted":
        raise DomainError("MODULE_ATTEMPT_REQUIRED", "需要本人同模块已实际提交的尝试。", 409)
    activity = app.learning._activity(db, actor, attempt.activity_id)
    if (
        activity.module_id != module.id
        or activity.specification.get("module_content_hash") != module.content_hash
        or attempt.specification_hash != activity.specification_hash
        or attempt.submission_hash
        != fingerprint(
            {
                "response": attempt.response,
                "transfer_notes": attempt.transfer_notes,
                "external_help": attempt.external_help,
                "coding_submission_id": attempt.submission_id,
            }
        )
    ):
        raise DomainError("MODULE_ATTEMPT_BINDING", "尝试与本模块或冻结提交不一致。", 409)
    return {
        "attempt_id": attempt.id,
        "activity_id": activity.id,
        "module_id": activity.module_id,
        "specification_hash": activity.specification_hash,
        "submission_hash": attempt.submission_hash,
        "state": attempt.state,
        "kind": activity.kind,
        "assistance_mode": app.learning._assistance_label(attempt),
        "assessment_state": "submitted_not_automatically_verified",
        "competence_verified": False,
    }


def require_ackable_task(task, attempt):
    """A generic ACK cannot stand in for mandatory or unsubmitted planned work."""
    if any(check.get("mandatory") is True for check in task.get("acceptance_checks", [])):
        raise DomainError(
            "MODULE_ACCEPTANCE_REQUIRED", "模块有强制验收条件，普通确认不能绕过。", 409
        )
    if task.get("kind", "reading") != "reading" and (
        not attempt or attempt["kind"] != task["kind"]
    ):
        raise DomainError(
            "MODULE_ATTEMPT_REQUIRED", "此模块包含实际活动，须绑定同类已提交尝试。", 409
        )


def _ack_result(app, db, actor, ack_id):
    ack = require_object(db, actor, ack_id, "module_completion_ack")
    data = ack.payload
    if data.get("principal_id") != actor or data.get("contract") != MODULE_ACK_CONTRACT:
        raise DomainError("NOT_FOUND", "模块确认回执不可访问。", 404)
    _, turn, snapshot = _session_in(db, actor, data["learning_session_id"], history=True)
    check_closure(db, actor, [ack.id])
    current = _delivery_in(app, db, actor, turn, snapshot)
    if current != data["delivery"]:
        raise DomainError("MODULE_ACK_BINDING", "原模块确认的交付已变化。", 409)
    if data.get("attempt"):
        module = require_object(db, actor, data["module_id"], "plan_module")
        if _attempt_in(app, db, actor, module, data["attempt"]["attempt_id"]) != data["attempt"]:
            raise DomainError("MODULE_ACK_BINDING", "原模块确认的尝试已变化。", 409)
    return {"ack_id": ack.id, "ack_hash": ack.content_hash, **deepcopy(data)}


def session_detail(app, actor, session_id):
    with app.db.read() as db:
        session, turn, snapshot = _session_in(db, actor, session_id, history=True)
        delivery = (
            _delivery_in(app, db, actor, turn, snapshot) if turn.status == "completed" else None
        )
        old = db.scalar(
            select(Idempotency).where(
                Idempotency.principal_id == actor,
                Idempotency.operation == "learning.module_session_ack",
                Idempotency.key == session_id,
            )
        )
        ack = _ack_result(app, db, actor, old.result["ack_id"]) if old else None
        profile = db.scalar(select(LearningProfile).where(LearningProfile.principal_id == actor))
        plan = db.get(LearningPlan, snapshot["module"]["plan_id"])
        active = bool(
            plan
            and profile.active_plan_id == plan.id
            and plan.active_version_id == snapshot["module"]["version_id"]
            and plan.revision == snapshot["module"]["plan_revision"]
            and profile.active_module_id == snapshot["module"]["module_id"]
        )
        module = require_object(db, actor, snapshot["module"]["module_id"], "plan_module")
        task = module.payload.get("task") or {}
        mandatory = any(
            check.get("mandatory") is True for check in task.get("acceptance_checks", [])
        )
        independent = bool(
            db.scalar(
                select(Workspace.id).where(
                    Workspace.principal_id == actor,
                    Workspace.assistance_mode == "independent",
                    Workspace.sealed.is_(False),
                )
            )
        )
        requirements = {
            "mandatory_acceptance_blocked": mandatory,
            "independent_workspace_active": independent,
            "required_submitted_attempt_kind": task.get("kind")
            if task.get("kind", "reading") != "reading"
            else None,
            "explicit_incomplete_acceptance_required": bool(
                delivery and delivery["requires_incomplete_acceptance"]
            ),
            "meaning": "eligibility_to_request_ack_not_verified_acceptance",
        }
        return {
            "learning_session_id": session.id,
            "session_hash": session.content_hash,
            **deepcopy(session.payload),
            "delivery_status": turn.status,
            "delivery": delivery,
            "module_ack": ack,
            "module_progress_status": "user_acknowledged"
            if ack
            else "awaiting_explicit_user_ack"
            if active
            else "historical_session",
            "current_binding_active": active,
            "can_acknowledge_module": bool(
                active
                and not ack
                and delivery
                and delivery["delivered_fact_ids"]
                and not mandatory
                and not independent
            ),
            "ack_requirements": requirements,
            "budget": snapshot["budget"],
            "current_profile_revision": profile.revision,
            "current_active_module_id": profile.active_module_id,
            "competence_verified": False,
        }


def acknowledge_module(app, actor, value: SessionModuleAckInput):
    with app.db.transaction() as db:
        return acknowledge_module_in(app, db, actor, value)


def acknowledge_module_in(app, db, actor, value: SessionModuleAckInput, *, practice=None):
    request = value.model_dump(mode="json")
    request_hash = fingerprint(request)
    ns = private_namespace(db, actor)
    if value.namespace_id != ns.id:
        raise DomainError("MODULE_ACK_SCOPE", "模块确认只能来自本人当前私人域。", 403)
    # Resolve the true immutable session identity without authority row locks.
    # A caller's other valid plan must never cause Plan A -> Profile -> Plan B.
    old_preview = db.scalar(
        select(Idempotency).where(
            Idempotency.principal_id == actor,
            Idempotency.operation == "learning.module_ack",
            Idempotency.key == value.idempotency_key,
        )
    )
    if old_preview and old_preview.request_hash != request_hash:
        raise DomainError("IDEMPOTENCY_CONFLICT", "确认键对应不同模块确认请求。", 409)
    preview = require_object(db, actor, value.learning_session_id, "learning_session")
    bound = preview.payload.get("module", {})
    if (
        preview.namespace_id != ns.id
        or preview.payload.get("principal_id") != actor
        or preview.payload.get("contract") != SESSION_ACTIVITY_CONTRACT
        or preview.payload.get("turn_id") != value.turn_id
        or bound.get("plan_id") != value.plan_id
        or bound.get("version_id") != value.version_id
        or bound.get("module_id") != value.module_id
        or preview.payload.get("session_tutor_binding", {}).get("snapshot_hash")
        != value.binding_snapshot_hash
    ):
        raise DomainError("MODULE_ACK_BINDING", "确认必须绑定该教学会话的确切身份。", 409)
    # Read the true immutable funding witness, never a caller-selected grant.
    # Paid work locks Funding before Plan; ACK must share that order too.
    from ..models import FundingAgreement
    from .session_tutor import _funding_receipt_in

    funding_witness = _funding_receipt_in(db, actor, value.turn_id)
    if funding_witness is not None:
        if funding_witness.result.get("binding") != preview.payload.get("session_tutor_binding"):
            raise DomainError("MODULE_ACK_BINDING", "确认资金见证不匹配。", 409)
        funding_id = funding_witness.result.get("authority", {}).get("funding_id")
        if funding_id:
            db.scalar(
                select(FundingAgreement)
                .where(FundingAgreement.id == funding_id)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
    # New-contract lock order: exact Funding -> true Plan -> Profile -> Turn. No Run,
    # Principal or Conversation write lock is acquired later. All checks below
    # are repeated under authority locks; the preflight is not mutation authority.
    plan = locked(db, LearningPlan, bound["plan_id"])
    if not plan or plan.principal_id != actor or plan.namespace_id != ns.id:
        raise DomainError("NOT_FOUND", "计划不可访问。", 404)
    profile = db.scalar(
        select(LearningProfile)
        .where(LearningProfile.principal_id == actor)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    old = db.scalar(
        select(Idempotency).where(
            Idempotency.principal_id == actor,
            Idempotency.operation == "learning.module_ack",
            Idempotency.key == value.idempotency_key,
        )
    )
    if old:
        if old.request_hash != request_hash:
            raise DomainError("IDEMPOTENCY_CONFLICT", "确认键对应不同模块确认请求。", 409)
        return {**_ack_result(app, db, actor, old.result["ack_id"]), "replayed": True}
    previous = db.scalar(
        select(Idempotency).where(
            Idempotency.principal_id == actor,
            Idempotency.operation == "learning.module_session_ack",
            Idempotency.key == value.learning_session_id,
        )
    )
    if previous:
        raise DomainError("MODULE_ALREADY_ACKNOWLEDGED", "该教学会话已有不可变确认回执。", 409)
    # Keep legacy Funding -> Plan -> Profile -> Turn order. Practice admission
    # holds this same Profile lock through its insert. A fresh read here fences
    # concurrent practice without adding a reverse Principal/Conversation lock.
    # Historical same-key ACK receipts have already returned above.
    app.practice_workflows.guard_direct_ack_in(db, actor, value, practice=practice)
    # Acknowledging an already delivered lesson does not spend model funds.
    # Validate immutable delivery even after its per-request grant expired/revoked;
    # active plan/profile, evidence closure and exact delivery hashes are checked below.
    if db.scalar(
        select(Workspace.id).where(
            Workspace.principal_id == actor,
            Workspace.assistance_mode == "independent",
            Workspace.sealed.is_(False),
        )
    ):
        raise DomainError("INDEPENDENT_ATTEMPT", "独立练习期间不能推进导师模块。")
    session, turn, snapshot = _session_in(db, actor, value.learning_session_id, history=True)
    turn = locked(db, Turn, turn.id)
    token, module_data = turn.focus[SESSION_TUTOR_KEY], snapshot["module"]
    if (
        value.turn_id != turn.id
        or value.plan_id != module_data["plan_id"]
        or value.version_id != module_data["version_id"]
        or value.module_id != module_data["module_id"]
        or value.binding_snapshot_hash != token["snapshot_hash"]
    ):
        raise DomainError("MODULE_ACK_BINDING", "确认必须绑定本次确切教学会话和模块。", 409)
    if (
        plan.revision != value.expected_plan_revision
        or profile.revision != value.expected_profile_revision
    ):
        raise DomainError("REVISION_CONFLICT", "计划或学习状态已变化，请重新查看。", 409)
    if (
        profile.active_plan_id != plan.id
        or profile.active_module_id != value.module_id
        or plan.active_version_id != value.version_id
    ):
        raise DomainError("MODULE_ACK_INACTIVE", "只能确认当前已采纳版本的活动模块。", 409)
    delivery = _delivery_in(app, db, actor, turn, snapshot)
    if (
        delivery["completion_assessment_hash"] != value.completion_assessment_hash
        or delivery["delivery_hash"] != value.delivery_hash
    ):
        raise DomainError("MODULE_ACK_BINDING", "确认没有绑定实际最终交付与完整性评估。", 409)
    if not delivery["delivered_fact_ids"]:
        raise DomainError("MODULE_ACK_NO_FACTS", "没有已交付的有效模块事实，不能确认完成。", 409)
    if delivery["requires_incomplete_acceptance"] and not value.accept_incomplete_material:
        raise DomainError(
            "MODULE_ACK_INCOMPLETE", "存在未完成或未核实内容，需本人明确接受这些不足。", 409
        )
    module = require_object(db, actor, value.module_id, "plan_module")
    task = module.payload.get("task") or {}
    # No generic user ACK can override a separately mandatory acceptance rule.
    # This first contract does not invent an evaluator for future mandatory rules.
    attempt = _attempt_in(app, db, actor, module, value.attempt_id)
    require_ackable_task(task, attempt)
    version = db.get(PlanVersion, value.version_id)
    position = version.module_ids.index(value.module_id)
    next_id = version.module_ids[position + 1] if position + 1 < len(version.module_ids) else None
    before_revision = profile.revision
    profile.active_module_id = next_id
    profile.revision += 1
    payload = {
        "contract": MODULE_ACK_CONTRACT,
        "principal_id": actor,
        "namespace_id": ns.id,
        "learning_session_id": session.id,
        "turn_id": turn.id,
        "plan_id": plan.id,
        "version_id": version.id,
        "module_id": module.id,
        "module_content_hash": module.content_hash,
        "session_tutor_binding": token,
        "request_hash": request_hash,
        "request": request,
        "privacy_generation": assert_personal_access(db, actor),
        "delivery": delivery,
        "attempt": attempt,
        "profile_revision_before": before_revision,
        "profile_revision_after": profile.revision,
        "next_active_module_id": next_id,
        "plan_progress_status": "finished_by_user_ack" if next_id is None else "next_module_active",
        "module_progress_status": "user_acknowledged",
        "module_completed": True,
        "completion_basis": "explicit_user_confirmation_not_competence",
        "confirmation_provenance": "synthetic_user_confirmation"
        if app.settings.mode == "test"
        else "authenticated_user_confirmation",
        "acceptance_passed": None,
        "competence_verified": False,
    }
    ack = register(db, actor, ns.id, "module_completion_ack", payload)
    depend(db, actor, ack.id, session.id, "acknowledges_session")
    for block in delivery["blocks"]:
        depend(db, actor, ack.id, block["block_id"], "acknowledges_delivery")
    for operation, key in (
        ("learning.module_ack", value.idempotency_key),
        ("learning.module_session_ack", session.id),
    ):
        db.add(
            Idempotency(
                principal_id=actor,
                operation=operation,
                key=key,
                request_hash=request_hash,
                result={"ack_id": ack.id},
            )
        )
    return {
        "ack_id": ack.id,
        "ack_hash": ack.content_hash,
        **deepcopy(payload),
        "replayed": False,
    }
