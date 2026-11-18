"""Version-selected user-goal planning authority and conservative material coverage."""

from copy import deepcopy

from ..errors import DomainError
from ..util import fingerprint
from .dependencies import check_closure, require_object

GOAL_CATALOG_VERSION = "learning-text-catalog-goal-amendment-v1"
GOAL_PLAN_CONTRACT = "learning-goal-amendment-plan-v1"


def objective_coverage(
    goal, *, publication_ids=(), selected_id=None, scope_hash=None, task_complete=False
):
    """Reviewed material is not certified depth or competence; whole-goal gaps remain."""
    return [
        dict(
            objective_id=item["objective_id"],
            origin=item["origin"],
            target_depth=item.get("target_depth"),
            learning_priority=item.get("learning_priority", "unspecified"),
            material_status=(
                "pending_mapping"
                if item.get("mapping", {}).get("status") == "pending_mapping"
                else "reviewed_material"
                if publication_ids and item["objective_id"] == selected_id and task_complete
                else "partial"
                if publication_ids and item["objective_id"] == selected_id
                else "unknown"
            ),
            publication_ids=list(publication_ids) if item["objective_id"] == selected_id else [],
            scope_hash=scope_hash if item["objective_id"] == selected_id else None,
            target_depth_verified=False,
            competence_verified=False,
        )
        for item in goal.get("user_learning_objectives", [])
    ]


def plan_marker(goal, selection_payload, dialogue_scope):
    scope = (dialogue_scope or {}).get("goal_build_scope")
    return {
        "contract": GOAL_PLAN_CONTRACT,
        "goal_version_id": goal["goal_version_id"],
        "version_hash": goal["version_hash"],
        "scope_hash": scope.get("scope_hash") if scope else None,
        "selection_payload_hash": fingerprint(selection_payload),
    }


def validate_plan_scope_in(db, actor, frozen):
    goal = frozen.get("goal") or {}
    marker = frozen.get("goal_amendment_plan")
    if goal.get("origin_contract") != "command-goal-v1":
        if marker is not None or frozen.get("text_catalog_version") == GOAL_CATALOG_VERSION:
            raise DomainError("PLAN_GOAL_SCOPE", "新增目标合同与目标来源不匹配。", 409)
        return
    selection = require_object(db, actor, frozen["selection_manifest_id"], "selection_manifest")
    check_closure(db, actor, [selection.id, goal["goal_version_id"]])
    if (
        marker != plan_marker(goal, selection.payload, frozen.get("dialogue_scope"))
        or frozen.get("text_catalog_version") != GOAL_CATALOG_VERSION
    ):
        raise DomainError("PLAN_GOAL_SCOPE", "新增目标计划的冻结选择已变化。", 409)
    expected = {
        "goal_version_id": goal["goal_version_id"],
        "version_hash": goal["version_hash"],
        "scope_hash": marker["scope_hash"],
    }
    if selection.payload.get("goal_amendment") != expected:
        raise DomainError("PLAN_GOAL_SCOPE", "知识选择未绑定精确新增目标版本。", 409)
    if frozen.get("dialogue_scope") is not None:
        from .workflow_continuation import validate_dialogue_goal

        validate_dialogue_goal(db, actor, frozen["dialogue_scope"])


def goal_payload(version, snapshot):
    return {
        "goal_version_id": version.id,
        "requirements": deepcopy(version.requirements),
        "source_text": version.source_text,
        "content_hash": version.content_hash,
        "is_requirement_not_ability": True,
        **{
            key: deepcopy(snapshot[key])
            for key in (
                "origin_contract",
                "version_hash",
                "parent_goal_version_id",
                "goal_addendum_id",
                "command_receipt_id",
                "command_action_index",
                "user_learning_objectives",
            )
        },
    }
