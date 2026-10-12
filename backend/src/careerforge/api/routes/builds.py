from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from careerforge.api.dependencies import Authenticated, Database, IdempotencyKey
from careerforge.api.routes.planning import wrap
from careerforge.application import build_service as service
from careerforge.application.ownership import owned
from careerforge.application.planning_service import objective_version
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.db.models import (
    Assessment,
    BuildProject,
    BuildTask,
    PlanDeltaProposal,
    Submission,
)
from careerforge.schemas.builds import (
    AssessmentInput,
    ConfirmationInput,
    ProjectInput,
    RevocationInput,
    SubmissionInput,
    TaskInput,
    TaskRevision,
    TransitionInput,
)
from careerforge.schemas.resources import Envelope

router = APIRouter(prefix="/api/v1", tags=["builds"])


@router.post("/build-projects", status_code=201, response_model=Envelope)
def create_project(
    body: ProjectInput, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
) -> dict[str, Any]:
    return wrap(request, service.create_project(db, user.id, body, key))


@router.get("/build-projects", response_model=Envelope)
def projects(
    request: Request,
    db: Database,
    user: Authenticated,
    cursor: UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = select(BuildProject).where(BuildProject.owner_id == user.id)
    if cursor:
        query = query.where(BuildProject.id > cursor)
    rows = list(db.scalars(query.order_by(BuildProject.id).limit(limit + 1)))
    values = []
    for row in rows[:limit]:
        try:
            value = service.project(db, user.id, row.id)
            values.append({"build_project_id": str(value.id), **value.payload})
        except BusinessError as error:
            if error.status != 404:
                raise
    return {
        **wrap(request, values),
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }


@router.get("/build-projects/{identifier}", response_model=Envelope)
def project(
    identifier: UUID, request: Request, db: Database, user: Authenticated
) -> dict[str, Any]:
    row = service.project(db, user.id, identifier)
    return wrap(request, {"build_project_id": str(row.id), **row.payload})


@router.post("/build-projects/{identifier}/tasks", status_code=201, response_model=Envelope)
def create_task(
    identifier: UUID,
    body: TaskInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.create_task(db, user.id, identifier, body, key))


@router.get("/build-tasks", response_model=Envelope)
def tasks(
    request: Request,
    db: Database,
    user: Authenticated,
    project_id: UUID | None = None,
    status: str | None = None,
    objective_id: UUID | None = None,
    cursor: UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = select(BuildTask).where(BuildTask.owner_id == user.id)
    if project_id:
        query = query.where(BuildTask.project_id == project_id)
    if status:
        query = query.where(BuildTask.status == status)
    if cursor:
        query = query.where(BuildTask.id > cursor)
    rows = list(db.scalars(query.order_by(BuildTask.id).limit(limit + 1)))
    values = []
    for row in rows[:limit]:
        try:
            value = service.task_detail(db, user.id, row.id)
            if (
                objective_id
                and objective_version(db, user.id, UUID(value["objective_version_id"])).objective_id
                != objective_id
            ):
                continue
            values.append(value)
        except BusinessError as error:
            if error.status != 404:
                raise
    return {
        **wrap(request, values),
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }


@router.get("/build-tasks/{identifier}", response_model=Envelope)
def task(identifier: UUID, request: Request, db: Database, user: Authenticated) -> dict[str, Any]:
    return wrap(request, service.task_detail(db, user.id, identifier))


@router.get("/build-tasks/{identifier}/versions/{version_id}", response_model=Envelope)
def version(
    identifier: UUID, version_id: UUID, request: Request, db: Database, user: Authenticated
) -> dict[str, Any]:
    row = service.task_version(db, user.id, version_id)
    if row.task_id != identifier:
        raise BusinessError(404, "NOT_FOUND", "资源不存在。")
    return wrap(
        request, {"task_version_id": str(row.id), "version_no": row.version_no, **row.payload}
    )


@router.post("/build-tasks/{identifier}/versions", status_code=201, response_model=Envelope)
def revise(
    identifier: UUID,
    body: TaskRevision,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.revise_task(db, user.id, identifier, body, key))


@router.post("/build-tasks/{identifier}/transition", response_model=Envelope)
def transition(
    identifier: UUID,
    body: TransitionInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.transition_task(db, user.id, identifier, body, key))


@router.post("/build-tasks/{identifier}/submissions", status_code=201, response_model=Envelope)
def submit(
    identifier: UUID,
    body: SubmissionInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.submit(db, user.id, identifier, body, key))


@router.get("/build-tasks/{identifier}/submissions", response_model=Envelope)
def submissions(
    identifier: UUID,
    request: Request,
    db: Database,
    user: Authenticated,
    cursor: UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    service.task(db, user.id, identifier)
    query = select(Submission).where(
        Submission.task_id == identifier, Submission.owner_id == user.id
    )
    if cursor:
        query = query.where(Submission.id > cursor)
    rows = list(db.scalars(query.order_by(Submission.id).limit(limit + 1)))
    return {
        **wrap(
            request,
            [
                {
                    "submission_id": str(r.id),
                    "submission_version": r.version_no,
                    "task_version_id": str(r.task_version_id),
                }
                for r in rows[:limit]
            ],
        ),
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }


@router.get("/submissions/{identifier}", response_model=Envelope)
def submission(
    identifier: UUID, request: Request, db: Database, user: Authenticated
) -> dict[str, Any]:
    row = service.submission(db, user.id, identifier)
    reviews = list(
        db.scalars(
            select(Assessment).where(
                Assessment.submission_id == row.id, Assessment.owner_id == user.id
            )
        )
    )
    return wrap(
        request,
        {
            "submission_id": str(row.id),
            "task_id": str(row.task_id),
            "submission_version": row.version_no,
            "task_version_id": str(row.task_version_id),
            **row.payload,
            "assessment_ids": [str(r.id) for r in reviews],
        },
    )


@router.post("/submissions/{identifier}/assessment-runs", status_code=202, response_model=Envelope)
def assess(
    identifier: UUID,
    body: AssessmentInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.start_assessment(db, user.id, identifier, body, key))


@router.get("/assessments/{identifier}", response_model=Envelope)
def assessment(
    identifier: UUID, request: Request, db: Database, user: Authenticated
) -> dict[str, Any]:
    return wrap(request, service.assessment_detail(db, user.id, identifier))


@router.post("/assessments/{identifier}/confirm", status_code=201, response_model=Envelope)
def confirm(
    identifier: UUID,
    body: ConfirmationInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.confirm(db, user.id, identifier, body, key))


@router.post("/assessments/{identifier}/revoke", response_model=Envelope)
def revoke(
    identifier: UUID,
    body: RevocationInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.revoke(db, user.id, identifier, body, key))


@router.get("/plan-deltas/{identifier}", response_model=Envelope)
def delta(identifier: UUID, request: Request, db: Database, user: Authenticated) -> dict[str, Any]:
    row = owned(db, PlanDeltaProposal, user.id, identifier)
    service.assessment(db, user.id, row.assessment_id)
    return wrap(request, {"plan_delta_proposal_id": str(row.id), **row.payload})
