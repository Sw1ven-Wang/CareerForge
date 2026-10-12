from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application.common import check_revision, fingerprint, idempotency
from careerforge.domain.errors import not_found
from careerforge.infrastructure.db.models import CareerGoal, CareerGoalVersion
from careerforge.schemas.resources import GoalInput, GoalRevision


def get_goal(db: Session, owner: UUID, identifier: UUID, *, lock: bool = False) -> CareerGoal:
    query = select(CareerGoal).where(CareerGoal.id == identifier, CareerGoal.owner_id == owner)
    if lock:
        query = query.with_for_update()
    goal = db.scalars(query).first()
    if goal is None:
        raise not_found()
    return goal


def goal_result(db: Session, goal: CareerGoal) -> dict[str, Any]:
    version = db.scalars(
        select(CareerGoalVersion)
        .where(CareerGoalVersion.goal_id == goal.id, CareerGoalVersion.owner_id == goal.owner_id)
        .order_by(CareerGoalVersion.version_no.desc())
        .limit(1)
    ).one_or_none()
    # Limit explicitly because every immutable version is retained.
    if version is None:
        raise not_found()
    return {
        "goal_id": str(goal.id),
        "goal_version_id": str(version.id),
        "version": goal.revision,
        "active_plan_version_id": None,
        **version.payload,
    }


def save_goal(
    db: Session, owner: UUID, value: GoalInput, key: str | None, identifier: UUID | None = None
) -> dict[str, Any]:
    operation = f"goals.{identifier}.versions" if identifier else "goals.create"
    record = idempotency(db, owner, operation, key, value.model_dump(mode="json"))
    if record.response_summary is not None:
        get_goal(db, owner, UUID(record.response_summary["goal_id"]))
        return record.response_summary
    if identifier:
        assert isinstance(value, GoalRevision)
        goal = get_goal(db, owner, identifier, lock=True)
        check_revision(goal.revision, value.expected_version)
        goal.revision += 1
    else:
        goal = CareerGoal(owner_id=owner)
        db.add(goal)
        db.flush()
    payload = value.model_dump(mode="json", exclude={"expected_version", "change_reason"})
    version = CareerGoalVersion(
        goal_id=goal.id,
        owner_id=owner,
        version_no=goal.revision,
        content_hash=fingerprint(payload),
        payload=payload,
        change_reason=value.change_reason if isinstance(value, GoalRevision) else None,
    )
    db.add(version)
    db.flush()
    result = {"goal_id": str(goal.id), "goal_version_id": str(version.id), "version": goal.revision}
    record.response_summary = result
    return result
