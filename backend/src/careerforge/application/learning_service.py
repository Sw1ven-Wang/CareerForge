from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application.common import check_revision, idempotency
from careerforge.application.ownership import owned
from careerforge.application.planning_service import criteria, exact_ids, objective_version
from careerforge.application.repository_service import citation_detail, recommendation, snapshot
from careerforge.application.run_service import create_run
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.db.models import (
    ArtifactFeedback,
    LearningProgress,
    LearningUnit,
    ProjectSelection,
)
from careerforge.schemas.learning import (
    LearningFeedback,
    LearningInput,
    LearningQuestion,
    ProgressInput,
)

VERSION = "learning-offline-v1"


def unit(db: Session, owner: UUID, identifier: UUID) -> LearningUnit:
    row = owned(db, LearningUnit, owner, identifier)
    snapshot(db, owner, row.snapshot_id)
    objective_version(db, owner, row.objective_version_id)
    for i in row.payload["citations"]:
        citation_detail(db, owner, UUID(i))
    return row


def validate(db: Session, owner: UUID, data: dict[str, Any]) -> None:
    selection = owned(db, ProjectSelection, owner, UUID(data["selection_id"]))
    recommendation(db, owner, selection.recommendation_id)
    obj = objective_version(db, owner, UUID(data["objective_version_ids"][0]))
    if str(obj.id) not in selection.payload["objective_version_ids"]:
        raise BusinessError(422, "SELECTION_OBJECTIVE_MISMATCH", "选择记录未覆盖此目标。")
    exact_ids(
        [c.id for c in criteria(db, owner, obj.id)],
        [UUID(i) for i in data["criterion_version_ids"]],
    )
    previous = data.get("previous_learning_unit_id")
    if previous:
        old = unit(db, owner, UUID(previous))
        if old.selection_id != selection.id or old.objective_version_id != obj.id:
            raise BusinessError(422, "LEARNING_SCOPE_MISMATCH", "改版须沿用目标和固定选择。")
    for identifier in data.get("feedback_ids", []):
        row = owned(db, ArtifactFeedback, owner, UUID(identifier))
        if row.artifact_kind != "learning_unit" or str(row.artifact_id) != previous:
            raise BusinessError(422, "FEEDBACK_SCOPE_MISMATCH", "反馈须属于上一学习单元。")


def start(db: Session, owner: UUID, body: LearningInput, key: str | None) -> dict[str, Any]:
    data = body.model_dump(mode="json")
    record = idempotency(db, owner, "learning.create", key, data)
    validate(db, owner, data)
    if record.response_summary:
        return record.response_summary
    obj = objective_version(db, owner, body.objective_version_ids[0])
    run = create_run(
        db,
        owner,
        "learning_path",
        VERSION,
        data,
        call_budget=0,
        goal_version_id=obj.goal_version_id,
    )
    record.response_summary = {"run_id": str(run.id), "version": run.version, "status": run.status}
    return record.response_summary


def detail(db: Session, owner: UUID, identifier: UUID) -> dict[str, Any]:
    row = unit(db, owner, identifier)
    progress = db.get(LearningProgress, row.id)
    feedback = list(
        db.scalars(
            select(ArtifactFeedback).where(
                ArtifactFeedback.owner_id == owner,
                ArtifactFeedback.artifact_kind == "learning_unit",
                ArtifactFeedback.artifact_id == row.id,
            )
        )
    )
    return {
        "learning_unit_id": str(row.id),
        "selection_id": str(row.selection_id),
        "supersedes_unit_id": str(row.supersedes_unit_id) if row.supersedes_unit_id else None,
        **row.payload,
        "progress": {"version": progress.revision, **progress.payload}
        if progress
        else {"version": 1, "completed_step_ids": []},
        "needs_review": bool(feedback),
        "feedback": [{"feedback_id": str(f.id), **f.payload} for f in feedback],
    }


def update_progress(
    db: Session, owner: UUID, identifier: UUID, body: ProgressInput, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"learning.{identifier}.progress", key, body.model_dump(mode="json")
    )
    row = unit(db, owner, identifier)
    progress = db.scalar(
        select(LearningProgress).where(LearningProgress.unit_id == identifier).with_for_update()
    )
    assert progress is not None
    if record.response_summary:
        return record.response_summary
    check_revision(progress.revision, body.expected_version)
    allowed = {s["step_id"] for s in row.payload["reading_steps"]}
    if (
        len(set(body.completed_step_ids)) != len(body.completed_step_ids)
        or not set(body.completed_step_ids) <= allowed
    ):
        raise BusinessError(422, "INVALID_READING_STEP", "阅读步骤无效。")
    progress.revision += 1
    progress.payload = body.model_dump(mode="json", exclude={"expected_version"})
    record.response_summary = {
        "version": progress.revision,
        **progress.payload,
        "competence_updated": False,
    }
    return record.response_summary


def feedback(
    db: Session, owner: UUID, identifier: UUID, body: LearningFeedback, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"learning.{identifier}.feedback", key, body.model_dump(mode="json")
    )
    row = unit(db, owner, identifier)
    if not set(map(str, body.citation_ids)) <= set(row.payload["citations"]):
        raise BusinessError(422, "CITATION_SCOPE_MISMATCH", "引用不属于此单元。")
    if record.response_summary:
        return record.response_summary
    item = ArtifactFeedback(
        owner_id=owner,
        artifact_kind="learning_unit",
        artifact_id=identifier,
        payload=body.model_dump(mode="json"),
    )
    db.add(item)
    db.flush()
    record.response_summary = {
        "feedback_id": str(item.id),
        "next_action": "explicit_learning_path_run",
    }
    return record.response_summary


def question(
    db: Session, owner: UUID, identifier: UUID, body: LearningQuestion, key: str | None
) -> dict[str, Any]:
    row = unit(db, owner, identifier)
    if body.context_step_id and body.context_step_id not in {
        s["step_id"] for s in row.payload["reading_steps"]
    }:
        raise BusinessError(422, "INVALID_READING_STEP", "追问步骤无效。")
    data = {
        "selection_id": str(row.selection_id),
        "objective_version_ids": [str(row.objective_version_id)],
        "criterion_version_ids": row.payload["criterion_version_ids"],
        "previous_learning_unit_id": str(row.id),
        "feedback_ids": [],
        **body.model_dump(mode="json"),
    }
    record = idempotency(db, owner, f"learning.{identifier}.question", key, data)
    if record.response_summary:
        return record.response_summary
    run = create_run(
        db,
        owner,
        "learning_path",
        VERSION,
        data,
        call_budget=0,
        goal_version_id=objective_version(db, owner, row.objective_version_id).goal_version_id,
    )
    record.response_summary = {"run_id": str(run.id), "version": run.version, "status": run.status}
    return record.response_summary
