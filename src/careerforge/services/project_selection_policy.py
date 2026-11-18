"""Versioned user screening rules, separate from immutable employer requirements."""

from sqlalchemy import select

from ..models import ObjectRow
from .dependencies import check_closure, depend, register, require_object
from .project_quality import validate_community_policy
from .research_coverage import read_goal_version


def current_policy_in(db, actor, goal_id):
    goal = read_goal_version(db, actor, goal_id)
    rows = db.scalars(
        select(ObjectRow)
        .where(
            ObjectRow.kind == "project_selection_policy",
            ObjectRow.namespace_id == goal["namespace_id"],
        )
        .order_by(ObjectRow.created_at.desc(), ObjectRow.id.desc())
    )
    for row in rows:
        if row.payload.get("goal_version_id") == goal_id:
            require_object(db, actor, row.id, "project_selection_policy")
            check_closure(db, actor, [row.id])
            return validate_community_policy(row.payload["policy"])
    return None


def record_policy_in(db, actor, goal_id, policy, source_text):
    goal = read_goal_version(db, actor, goal_id)
    validate_community_policy(policy)
    obj = register(
        db,
        actor,
        goal["namespace_id"],
        "project_selection_policy",
        {
            "goal_version_id": goal_id,
            "policy": policy,
            "source": "user_project_screening_preference",
            "source_text": source_text,
            "employer_requirements_unchanged": True,
        },
    )
    depend(db, actor, obj.id, goal_id, "binds_goal_version")
    return obj.id
