"""Atomic legacy dialogue actions for the local learning desk.

Effects share the native ConversationActionReceipt transaction. No free text is
silently submitted as an exercise; submission and module acknowledgment require
explicit caller contracts. Existing graph workflows retain their own contracts.
"""

from ..contracts_assessment_v166 import AssessmentInput
from ..errors import DomainError
from ..models import AgentRun
from ..models_learning_ext import LearningAttempt
from .learning_ext import _profile
from .session_progress import acknowledge_module_in
from .workflow_continuation import _wait


def apply_learning_action(app, db, actor, conv, workflow, message, name):
    if workflow and workflow.learning_graph_version:
        raise DomainError("PRACTICE_CALLER_CONTRACT", "图工作流请使用其绑定的练习协议。", 409)
    if workflow and workflow.control_state != "active":
        raise DomainError("WORKFLOW_NOT_ACTIVE", "请先恢复当前工作流。", 409)
    if name == "use_reviewed_material":
        ids = list(dict.fromkeys(message.publication_ids))
        if not ids:
            raise DomainError("MATERIAL_REQUIRED", "请选择已审核学习材料。", 422)
        valid = app.continuation.publications(db, actor, ids)
        if set(valid) != set(ids):
            raise DomainError("EVIDENCE_UNAVAILABLE", "所选材料当前不可用。", 409)
        if (
            workflow is None
            or workflow.target.get("proposal")
            or workflow.target.get("accepted_plan")
        ):
            workflow = app.workflows.create_in(db, conv, parent=workflow)
        sources = list(
            dict.fromkeys(
                app.knowledge.publication_pack(db, actor, pid)["source_id"] for pid in ids
            )
        )
        workflow.target = {
            **workflow.target,
            "publication_ids": ids,
            "source_ids": sources,
            "coverage": "partial",
            "desk_reading_plan": "desk-reading-v1",
            "funding_ref": message.authorization_ref,
        }
        app.workflows.focus_in(db, conv, workflow)
        return workflow, app.continuation.propose_plan(
            db, actor, conv, workflow, message.authorization_ref
        )
    if not workflow or not workflow.target.get("accepted_plan"):
        raise DomainError("PLAN_REQUIRED", "请先创建并确认学习计划。", 409)
    if name == "advance_learning_module":
        value = message.module_ack
        if value is None or value.turn_id != workflow.target.get("last_tutor_turn_id"):
            raise DomainError("MODULE_ACK_BINDING", "请确认当前模块最近一次讲解。", 409)
        if value.plan_id != workflow.target["accepted_plan"]["plan_id"]:
            raise DomainError("MODULE_ACK_BINDING", "确认不属于当前计划。", 409)
        result = acknowledge_module_in(app, db, actor, value)
        _wait(
            workflow,
            "learning_completed" if not result["next_active_module_id"] else "tutoring",
            None,
            last_module_ack=result,
            attempt_id=None,
            activity_id=None,
            assessment_run_id=None,
            last_tutor_turn_id=None,
        )
        return workflow, {
            "reply": "已记录本模块阅读完成；这不是能力掌握认证。"
            + (
                "全部模块已走完，可回顾反馈。"
                if not result["next_active_module_id"]
                else "已切换下一模块，请开始讲解。"
            ),
            "module_ack": result,
        }
    attempt_id = workflow.target.get("attempt_id")
    attempt = db.get(LearningAttempt, attempt_id) if attempt_id else None
    profile = _profile(db, actor)
    if not attempt or attempt.principal_id != actor:
        raise DomainError("ATTEMPT_REQUIRED", "请先说“开始练习”，再提交答案。", 409)
    activity = app.learning._activity(db, actor, attempt.activity_id)
    if activity.module_id != profile.active_module_id:
        raise DomainError("ATTEMPT_SCOPE", "练习不属于当前模块。", 409)
    if name == "submit_practice_answer":
        if message.submission is None:
            raise DomainError("SUBMISSION_REQUIRED", "请以“提交答案：”开头输入答案。", 422)
        result = app.learning.submit_attempt_in(db, actor, attempt.id, message.submission)
        _wait(workflow, "practice_submitted", "assess_practice")
        return workflow, {
            "reply": "答案已保存。可说“评估我的答案”获得逐项建议；评估不会自动宣称掌握。",
            "submission": result,
        }
    if not message.authorization_ref:
        return workflow, {
            "status": "waiting_confirmation",
            "reply": "请开启 AI 并再次发送“评估我的答案”，授权本次评估。",
        }
    result = app.assessments.start_in(
        db,
        actor,
        AssessmentInput(
            namespace_id=workflow.namespace_id,
            funding_id=message.authorization_ref,
            attempt_id=attempt.id,
            idempotency_key="desk-assess:" + message.idempotency_key,
        ),
    )
    app.workflows.link_in(db, workflow, db.get(AgentRun, result["run_id"]), "assessment")
    _wait(workflow, "assessing", None, assessment_run_id=result["run_id"])
    return workflow, {"reply": "正在按冻结练习的评分标准评估答案。", "run_refs": [result["run_id"]]}
