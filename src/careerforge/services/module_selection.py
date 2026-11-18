"""Explicit active-module CAS; neither a completion ACK nor competence evidence."""

from sqlalchemy import select

from ..contracts_conversation_actions_v3 import ModuleSelectionV1
from ..db import locked
from ..errors import DomainError
from ..models import AgentRun, LearningPlan, ObjectRow, PlanVersion, Turn
from .dependencies import check_closure, require_object
from .learning_binding import profile_in, require_assistance_in
from .session_progress import _ack_result


def select_module_in(app, db, actor, conv, workflow, receipt, index, value):
    if not isinstance(value, ModuleSelectionV1):
        raise DomainError("LEARNING_CALLER_CONTRACT", "模块选择需要明确版本化输入。", 422)
    app.practice_workflows.require_no_active_in(db, conv.id)
    if app.learning_requests.pending_in(db, conv.id):
        raise DomainError("LEARNING_REQUEST_PENDING", "当前学习请求完成后才能切换模块。", 409)
    # Direct/native Tutor endpoints remain separate, but their live Turn cannot be superseded.
    from .runtime import TERMINAL

    turns = list(db.scalars(select(Turn).where(Turn.conversation_id == conv.id)))
    if any(
        turn.status in {"queued", "running"}
        or (turn.run_id and db.get(AgentRun, turn.run_id).status not in TERMINAL)
        for turn in turns
    ):
        raise DomainError("CONVERSATION_BUSY", "现有导师运行完成后才能切换模块。", 409)
    require_assistance_in(db, actor)
    execution = app.session_workflows.current(db, workflow)
    app.learning_workflows.validate_in(db, workflow, execution)
    accepted = execution.binding_payload["accepted"]
    if (value.plan_id, value.version_id, value.expected_plan_revision) != (
        accepted["plan_id"],
        accepted["version_id"],
        accepted["plan_revision"],
    ):
        raise DomainError("LEARNING_PLAN_CHANGED", "选择不属于当前已采纳计划。", 409)
    plan = locked(db, LearningPlan, value.plan_id)
    if plan.revision != value.expected_plan_revision or plan.active_version_id != value.version_id:
        raise DomainError("LEARNING_PLAN_CHANGED", "当前已采纳计划已变化，请明确准入新版本。", 409)
    profile = profile_in(db, actor)
    version = db.get(PlanVersion, value.version_id)
    if (
        profile.revision != value.expected_profile_revision
        or conv.revision != value.expected_focus_revision
        or profile.active_plan_id != plan.id
    ):
        raise DomainError("REVISION_CONFLICT", "模块选择的会话或档案版本已变化。", 409)
    if value.module_id not in version.module_ids:
        raise DomainError("NOT_FOUND", "模块不属于计划。", 404)
    module = require_object(db, actor, value.module_id, "plan_module")
    app.learning._validated_plan_manifest(db, actor, version)
    check_closure(db, actor, [module.id])
    position = version.module_ids.index(module.id)
    prerequisites = set(module.payload.get("prerequisite_module_ids", []))
    # A mandatory earlier module cannot be bypassed through selection.
    for previous in version.module_ids[:position]:
        content = require_object(db, actor, previous, "plan_module").payload
        if any(
            item.get("mandatory") is True
            for item in (content.get("task") or {}).get("acceptance_checks", [])
        ):
            prerequisites.add(previous)
    for required in prerequisites:
        acknowledgements = list(
            db.scalars(
                select(ObjectRow).where(
                    ObjectRow.namespace_id == conv.namespace_id,
                    ObjectRow.kind == "module_completion_ack",
                )
            )
        )
        matching = [
            item
            for item in acknowledgements
            if item.payload.get("module_id") == required
            and item.payload.get("version_id") == value.version_id
        ]
        if not matching:
            raise DomainError("LEARNING_PREREQUISITE", "前置模块尚无真实完成回执。", 409)
        _ack_result(app, db, actor, matching[-1].id)
    before = profile.revision
    profile.active_module_id = module.id
    profile.revision += 1
    workflow.revision += 1
    conv.focus = {
        key: item
        for key, item in conv.focus.items()
        if key
        not in {
            "evidence_answer_state",
            "evidence_continuity_contract",
            "last_retrieval_manifest_id",
            "resolved_scope",
            "project_source_ids",
            "topic_keys",
            "retrieval_topic_keys",
            "topic_hints",
        }
    }
    conv.revision += 1
    result = {
        "module_id": module.id,
        "focus_revision": conv.revision,
        "profile_revision_before": before,
        "profile_revision": profile.revision,
        "competence_verified": False,
        "module_completed": False,
        "reply": "已明确选择学习模块，不代表已完成或掌握。",
    }
    app.workflows.emit_in(
        db,
        conv,
        "workflow.module_selected",
        f"module-selection:{receipt.id}:{index}",
        workflow,
        module_id=module.id,
        profile_revision=profile.revision,
    )
    app.learning_workflows.wake_in(db, workflow, execution, "module_selection")
    return result


def selection_from_message_in(app, db, actor, conv, workflow, message, selector):
    if not selector:
        raise DomainError("LEARNING_MODULE_REQUIRED", "请明确模块序号、名称或ID。", 422)
    execution = app.session_workflows.current(db, workflow)
    app.learning_workflows.validate_in(db, workflow, execution)
    accepted = execution.binding_payload["accepted"]
    plan = locked(db, LearningPlan, accepted["plan_id"])
    if (
        plan.revision != accepted["plan_revision"]
        or plan.active_version_id != accepted["version_id"]
    ):
        raise DomainError("LEARNING_PLAN_CHANGED", "当前已采纳计划已变化，请明确准入新版本。", 409)
    profile = profile_in(db, actor)
    ids = accepted["module_ids"]
    if selector.isascii() and selector.isdecimal():
        position = int(selector) - 1
        matches = [ids[position]] if 0 <= position < len(ids) else []
    else:
        matches = []
        for identifier in ids:
            module = require_object(db, actor, identifier, "plan_module")
            if selector == identifier or selector == module.payload.get("title"):
                matches.append(identifier)
    if len(matches) != 1:
        raise DomainError(
            "LEARNING_MODULE_AMBIGUOUS", "模块选择没有唯一匹配，请使用明确序号或ID。", 409
        )
    return ModuleSelectionV1(
        expected_revision=workflow.revision,
        idempotency_key=message.idempotency_key,
        plan_id=plan.id,
        version_id=accepted["version_id"],
        module_id=matches[0],
        expected_plan_revision=accepted["plan_revision"],
        expected_profile_revision=profile.revision,
        expected_focus_revision=message.expected_focus_revision,
    )
