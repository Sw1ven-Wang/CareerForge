"""Read immutable accepted-plan authority; never manufacture historical acceptance."""

from sqlalchemy import select

from ..contracts_session_tutor_v166 import SessionTutorTurnInput
from ..db import locked
from ..errors import DomainError
from ..models import LearningPlan, ObjectRow, PlanVersion, Workspace
from ..models_learning_ext import LearningProfile, PlanProposalManifest
from ..util import fingerprint
from .dependencies import check_closure
from .privacy import assert_personal_access
from .session_plans import SESSION_PLAN_CONTRACT
from .session_tutor import _authority


def profile_in(db, actor):
    profile = db.scalar(
        select(LearningProfile)
        .where(LearningProfile.principal_id == actor)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if profile is None:
        raise DomainError("LEARNING_PROFILE", "缺少学习档案。", 409)
    return profile


def accepted_binding_in(
    app, db, actor, conv, plan_id, version_id, revision, *, module_id=None, require_current=True
):
    # Immutable owner validation grants no write/paid authority. Avoid acquiring
    # mutable selection locks before later control roots (Funding -> Run). New
    # admissions and dispatches still take the real Plan lock after funding.
    plan = (
        locked(db, LearningPlan, plan_id)
        if require_current
        else db.get(LearningPlan, plan_id, populate_existing=True)
    )
    version = db.get(PlanVersion, version_id)
    if (
        not plan
        or plan.principal_id != actor
        or plan.namespace_id != conv.namespace_id
        or not version
        or version.plan_id != plan.id
    ):
        raise DomainError("NOT_FOUND", "计划不可访问。", 404)
    if require_current and (plan.active_version_id != version.id or plan.revision != revision):
        raise DomainError("LEARNING_PLAN_CHANGED", "已采纳计划发生变化。", 409)
    proposal = app.learning._validated_plan_manifest(db, actor, version)
    if (
        not proposal
        or proposal.payload.get("input", {}).get("session_contract") != SESSION_PLAN_CONTRACT
    ):
        raise DomainError("SESSION_TUTOR_PLAN", "计划没有完整单次教学合同。", 409)
    frozen = proposal.payload["input"]
    goal = frozen.get("goal")
    scope = frozen.get("dialogue_scope")
    # Validate scope even when no extra command-goal markers are present.
    if scope:
        from .workflow_continuation import validate_dialogue_goal

        if validate_dialogue_goal(db, actor, scope) != (goal or {}).get("goal_version_id"):
            raise DomainError("WORKFLOW_GOAL_SCOPE", "计划目标与原工作流不一致。", 409)
    if goal:
        from .research_coverage import read_goal_version

        read_goal_version(db, actor, goal["goal_version_id"])
        # Existing plan validator compares full source goal, including command-goal reverse links.
    check_closure(db, actor, version.module_ids)
    generation = assert_personal_access(db, actor)
    if module_id is not None:
        value = SessionTutorTurnInput(
            question="validate",
            expected_revision=conv.revision,
            idempotency_key="learning-binding",
            namespace_id=conv.namespace_id,
            plan_id=plan.id,
            version_id=version.id,
            module_id=module_id,
            expected_plan_revision=revision,
        )
        _authority(db, actor, conv, value, history=not require_current)
    return {
        "plan_id": plan.id,
        "version_id": version.id,
        "plan_revision": revision,
        "proposal_manifest_id": proposal.id,
        "proposal_manifest_hash": proposal.content_hash,
        "proposal_run_id": proposal.run_id,
        "module_ids": version.module_ids,
        "module_hashes": proposal.payload["module_hashes"],
        "goal": goal_reference(goal),
        "dialogue_scope": scope_reference(scope),
        "source_jd_revision_id": ((goal or {}).get("requirements") or {}).get("jd_revision_id"),
        "privacy_generation": generation,
    }


def require_assistance_in(db, actor):
    if db.scalar(
        select(Workspace.id).where(
            Workspace.principal_id == actor,
            Workspace.assistance_mode == "independent",
            Workspace.sealed.is_(False),
        )
    ):
        raise DomainError("INDEPENDENT_ATTEMPT", "独立练习期间不能启动导师。", 409)


def validate_persisted_owner_in(db, workflow, execution, *, access=True):
    """Selection-independent immutable authority for paid recovery; never new admission."""
    from ..models import GoalVersion
    from ..models_dialogue import ConversationActionReceipt, WorkflowExecution
    from ..workflows.registry import validate_session_binding

    validate_session_binding(execution)
    binding = execution.binding_payload
    if (
        execution.workflow_type != "learning_session"
        or execution.state_schema_version != 1
        or binding.get("contract") != "learning-session-binding-v1"
        or binding.get("execution_id") != execution.id
        or binding.get("workflow_id") != workflow.id
        or binding.get("conversation_id") != workflow.conversation_id
        or binding.get("principal_id") != workflow.principal_id
        or binding.get("namespace_id") != workflow.namespace_id
        or binding.get("learning_graph_version") != execution.graph_version
        or binding.get("tutor_graph_version") != "learning-tutor-v1"
        or execution.goal_version_id != workflow.goal_version_id
        or execution.source_jd_revision_id != workflow.source_jd_revision_id
    ):
        raise DomainError("LEARNING_BINDING", "学习执行固定身份不一致。", 409)
    accepted = binding["accepted"]
    plan, version = (
        db.get(LearningPlan, accepted["plan_id"]),
        db.get(PlanVersion, accepted["version_id"]),
    )
    proposal = db.get(PlanProposalManifest, accepted["proposal_manifest_id"])
    if (
        not plan
        or plan.principal_id != workflow.principal_id
        or plan.namespace_id != workflow.namespace_id
        or not version
        or version.plan_id != plan.id
        or version.module_ids != accepted["module_ids"]
        or not proposal
        or proposal.plan_version_id != version.id
        or proposal.principal_id != workflow.principal_id
        or proposal.content_hash != fingerprint(proposal.payload)
        or proposal.content_hash != accepted["proposal_manifest_hash"]
        or proposal.run_id != accepted["proposal_run_id"]
        or proposal.payload.get("module_hashes") != accepted["module_hashes"]
        or goal_reference(proposal.payload.get("input", {}).get("goal")) != accepted["goal"]
        or scope_reference(proposal.payload.get("input", {}).get("dialogue_scope"))
        != accepted["dialogue_scope"]
        or accepted["privacy_generation"] != assert_personal_access(db, workflow.principal_id)
    ):
        raise DomainError("LEARNING_BINDING", "已采纳计划的不可变内容发生变化。", 409)
    for identifier, digest in accepted["module_hashes"].items():
        module = db.get(ObjectRow, identifier)
        if (
            not module
            or module.content_hash != digest
            or module.content_hash != fingerprint(module.payload)
        ):
            raise DomainError("LEARNING_BINDING", "固定模块内容已变化。", 409)
    if access:
        check_closure(db, workflow.principal_id, accepted["module_ids"])
    goal = proposal.payload.get("input", {}).get("goal")
    if (goal or {}).get("goal_version_id") != workflow.goal_version_id:
        raise DomainError("WORKFLOW_GOAL_SCOPE", "固定目标不一致。", 409)
    if goal and access:
        from .goal_plan_v1 import goal_payload, validate_plan_scope_in
        from .research_coverage import read_goal_version

        row = db.get(GoalVersion, workflow.goal_version_id)
        snapshot = read_goal_version(db, workflow.principal_id, workflow.goal_version_id)
        validate_plan_scope_in(db, workflow.principal_id, proposal.payload["input"])
        if row.origin_contract == "command-goal-v1":
            if goal_payload(row, snapshot) != goal:
                raise DomainError("PLAN_GOAL_CHANGED", "新增目标固定内容已变化。", 409)
        elif row.content_hash != goal.get("content_hash") or row.requirements != goal.get(
            "requirements"
        ):
            raise DomainError("PLAN_GOAL_CHANGED", "岗位目标固定内容已变化。", 409)
    if accepted["dialogue_scope"] and access:
        from .workflow_continuation import validate_dialogue_goal

        validate_dialogue_goal(
            db, workflow.principal_id, proposal.payload["input"]["dialogue_scope"]
        )
    origin = binding.get("origin")
    if origin == "knowledge_handoff":
        predecessor = (
            db.get(WorkflowExecution, execution.predecessor_execution_id)
            if execution.predecessor_execution_id
            else None
        )
        if (
            execution.execution_generation != 2
            or execution.admission_receipt_id
            or execution.admission_action_index is not None
            or not predecessor
            or predecessor.workflow_id != workflow.id
            or predecessor.status != "completed"
            or predecessor.workflow_type != "knowledge_session"
            or predecessor.graph_version != "knowledge-session-v2"
            or predecessor.state_schema_version != 1
            or predecessor.execution_generation != 1
            or not predecessor.terminal_checkpoint_id
            or fingerprint(predecessor.binding_payload) != predecessor.goal_binding_hash
            or predecessor.goal_version_id != workflow.goal_version_id
        ):
            raise DomainError("LEARNING_ORIGIN", "知识完成来源不一致。", 409)
        from .learning_execution import LearningExecutions

        if binding.get("predecessor") != LearningExecutions.predecessor_binding(predecessor):
            raise DomainError("LEARNING_ORIGIN", "知识终态见证已变化。", 409)
        receipt_id, index = predecessor.acceptance_receipt_id, predecessor.acceptance_action_index
        kind = "accept_plan"
    elif origin == "accepted_plan_admission":
        if (
            execution.execution_generation != 1
            or execution.predecessor_execution_id
            or "predecessor" in binding
        ):
            raise DomainError("LEARNING_ORIGIN", "新准入来源不一致。", 409)
        receipt_id, index, kind = (
            execution.admission_receipt_id,
            execution.admission_action_index,
            "start_learning_session",
        )
        if binding.get("admission") != {"receipt_id": receipt_id, "action_index": index}:
            raise DomainError("LEARNING_ORIGIN", "新准入见证已变化。", 409)
    else:
        raise DomainError("LEARNING_ORIGIN", "未知来源。", 409)
    # A self-consistent L hash cannot invent a later acceptance revision. The
    # original initial request is independently anchored in its immutable action
    # outcome. Validate that exact chain for both honest admission origins.
    from ..models_dialogue import WorkflowTutorRequest
    from .learning_requests import receipt_in

    initial = db.scalar(
        select(WorkflowTutorRequest)
        .where(
            WorkflowTutorRequest.workflow_id == workflow.id,
            WorkflowTutorRequest.kind == "initial",
        )
        .execution_options(populate_existing=True)
    )
    if (
        initial is None
        or initial.sequence != 1
        or initial.conversation_id != workflow.conversation_id
        or initial.execution_id != execution.id
        or initial.predecessor_request_id is not None
        or initial.predecessor_turn_id is not None
        or (initial.action_receipt_id, initial.action_index) != (receipt_id, index)
        or initial.predecessor_execution_id != execution.predecessor_execution_id
        or initial.request.get("accepted") != accepted
    ):
        raise DomainError("LEARNING_ORIGIN", "学习绑定没有原始采纳请求的精确见证。", 409)
    receipt_in(db, initial)
    receipt = db.get(ConversationActionReceipt, receipt_id) if receipt_id else None
    results = receipt.result.get("action_receipts", []) if receipt else []
    if (
        not receipt
        or receipt.conversation_id != workflow.conversation_id
        or receipt.action_plan_hash != fingerprint(receipt.plan)
        or type(index) is not int
        or index < 0
        or index >= len(results)
        or results[index].get("action_type") != kind
        or results[index].get("workflow_id") != workflow.id
        or results[index].get("status") != "applied"
    ):
        raise DomainError("LEARNING_ORIGIN", "原准入动作回执无效。", 409)
    if kind == "accept_plan" and (
        results[index].get("version_id") != accepted["version_id"]
        or results[index].get("proposal_run_id") != accepted["proposal_run_id"]
    ):
        raise DomainError("LEARNING_ORIGIN", "原采纳未确认固定计划。", 409)


def goal_reference(goal):
    return (
        {
            "goal_version_id": goal["goal_version_id"],
            "origin_contract": goal.get("origin_contract", "run-goal-v1"),
            "goal_hash": fingerprint(goal),
        }
        if goal
        else None
    )


def scope_reference(scope):
    return (
        {"workflow_id": scope["workflow_id"], "scope_hash": fingerprint(scope)} if scope else None
    )
