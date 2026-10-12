from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from careerforge.api.dependencies import Authenticated, Database, IdempotencyKey
from careerforge.api.routes.planning import wrap
from careerforge.application import learning_service as service
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.db.models import LearningUnit
from careerforge.schemas.learning import (
    LearningFeedback,
    LearningInput,
    LearningQuestion,
    ProgressInput,
)
from careerforge.schemas.resources import Envelope

router = APIRouter(prefix="/api/v1", tags=["learning"])


@router.post("/learning-path-runs", status_code=202, response_model=Envelope)
def create(
    body: LearningInput, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
) -> dict[str, Any]:
    return wrap(request, service.start(db, user.id, body, key))


@router.get("/learning-units", response_model=Envelope)
def listing(
    request: Request,
    db: Database,
    user: Authenticated,
    objective_version_id: UUID | None = None,
    repository_snapshot_id: UUID | None = None,
    cursor: UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = select(LearningUnit).where(LearningUnit.owner_id == user.id)
    if objective_version_id:
        query = query.where(LearningUnit.objective_version_id == objective_version_id)
    if repository_snapshot_id:
        query = query.where(LearningUnit.snapshot_id == repository_snapshot_id)
    if cursor:
        query = query.where(LearningUnit.id > cursor)
    rows = list(db.scalars(query.order_by(LearningUnit.id).limit(limit + 1)))
    values = []
    for row in rows[:limit]:
        try:
            values.append(service.detail(db, user.id, row.id))
        except BusinessError as error:
            if error.status != 404:
                raise
    return {
        **wrap(request, values),
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }


@router.get("/learning-units/{identifier}", response_model=Envelope)
def detail(identifier: UUID, request: Request, db: Database, user: Authenticated) -> dict[str, Any]:
    return wrap(request, service.detail(db, user.id, identifier))


@router.post("/learning-units/{identifier}/questions", status_code=202, response_model=Envelope)
def question(
    identifier: UUID,
    body: LearningQuestion,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.question(db, user.id, identifier, body, key))


@router.patch("/learning-units/{identifier}/progress", response_model=Envelope)
def progress(
    identifier: UUID,
    body: ProgressInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.update_progress(db, user.id, identifier, body, key))


@router.post("/learning-units/{identifier}/feedback", status_code=201, response_model=Envelope)
def feedback(
    identifier: UUID,
    body: LearningFeedback,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.feedback(db, user.id, identifier, body, key))
