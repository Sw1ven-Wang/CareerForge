from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from careerforge.api.dependencies import Authenticated, Database, IdempotencyKey
from careerforge.application import planning_service as service
from careerforge.application.ownership import owned
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.db.models import (
    LearningObjective,
    LearningPlan,
    LearningPlanVersion,
    PersonalEvidence,
    PersonalEvidenceVersion,
)
from careerforge.schemas.planning import (
    AlignmentInput,
    EvidenceInput,
    EvidenceRevision,
    ObjectiveInput,
    ObjectiveRevision,
    PlanAdoption,
    PlanObjectiveRevision,
    PlanRevision,
)
from careerforge.schemas.resources import Envelope, VersionInput

router = APIRouter(prefix="/api/v1", tags=["planning"])


def wrap(request: Request, value: Any) -> dict[str, Any]:
    return {"data": value, "request_id": request.state.request_id}


@router.post("/evidence", status_code=201, response_model=Envelope)
def create_evidence(
    body: EvidenceInput, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
) -> dict[str, Any]:
    return wrap(request, service.save_evidence(db, user.id, body, key))


@router.get("/evidence", response_model=Envelope)
def evidence_list(
    request: Request,
    db: Database,
    user: Authenticated,
    cursor: UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = select(PersonalEvidence).where(
        PersonalEvidence.owner_id == user.id, PersonalEvidence.revoked_at.is_(None)
    )
    if cursor:
        query = query.where(PersonalEvidence.id > cursor)
    roots = list(db.scalars(query.order_by(PersonalEvidence.id).limit(limit + 1)))
    data = []
    for root in roots[:limit]:
        row = db.scalar(
            select(PersonalEvidenceVersion)
            .where(PersonalEvidenceVersion.evidence_id == root.id)
            .order_by(PersonalEvidenceVersion.version_no.desc())
            .limit(1)
        )
        assert row is not None
        try:
            service.evidence_version(db, user.id, row.id)
        except BusinessError as error:
            if error.status == 404:
                continue
            raise
        data.append(
            {
                "evidence_id": str(root.id),
                "evidence_version_id": str(row.id),
                "version": root.revision,
                **row.payload,
            }
        )
    return {
        **wrap(request, data),
        "next_cursor": str(roots[limit - 1].id) if len(roots) > limit else None,
    }


@router.get("/evidence/{identifier}", response_model=Envelope)
def evidence_detail(
    identifier: UUID, request: Request, db: Database, user: Authenticated
) -> dict[str, Any]:
    root = owned(db, PersonalEvidence, user.id, identifier)
    rows = list(
        db.scalars(
            select(PersonalEvidenceVersion)
            .where(PersonalEvidenceVersion.evidence_id == root.id)
            .order_by(PersonalEvidenceVersion.version_no.desc())
        )
    )
    data = [
        {
            "evidence_version_id": str(v.id),
            "version_no": v.version_no,
            **service.evidence_version(db, user.id, v.id).payload,
        }
        for v in rows
    ]
    return wrap(request, {"evidence_id": str(root.id), "version": root.revision, "versions": data})


@router.post("/evidence/{identifier}/versions", status_code=201, response_model=Envelope)
def evidence_revision(
    identifier: UUID,
    body: EvidenceRevision,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.save_evidence(db, user.id, body, key, identifier))


@router.delete("/evidence/{identifier}", status_code=202, response_model=Envelope)
def evidence_delete(
    identifier: UUID,
    body: VersionInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(
        request, service.revoke_evidence(db, user.id, identifier, body.expected_version, key)
    )


@router.post("/learning-objectives", status_code=201, response_model=Envelope)
def objective_create(
    body: ObjectiveInput, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
) -> dict[str, Any]:
    return wrap(request, service.save_objective(db, user.id, body, key))


@router.post("/learning-objectives/{identifier}/versions", status_code=201, response_model=Envelope)
def objective_revision(
    identifier: UUID,
    body: ObjectiveRevision,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.save_objective(db, user.id, body, key, identifier))


@router.get("/learning-objectives", response_model=Envelope)
def objective_list(
    request: Request,
    db: Database,
    user: Authenticated,
    goal_version_id: UUID | None = None,
    cursor: UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = select(LearningObjective).where(LearningObjective.owner_id == user.id)
    if cursor:
        query = query.where(LearningObjective.id > cursor)
    roots = list(db.scalars(query.order_by(LearningObjective.id).limit(limit + 1)))
    data = []
    for r in roots[:limit]:
        try:
            data.append(service.objective_result(db, user.id, service.latest_objective(db, r)))
        except BusinessError as error:
            if error.status != 404:
                raise
    if goal_version_id:
        data = [r for r in data if r["goal_version_id"] == str(goal_version_id)]
    return {
        **wrap(request, data),
        "next_cursor": str(roots[limit - 1].id) if len(roots) > limit else None,
    }


@router.get("/learning-objectives/{identifier}", response_model=Envelope)
def objective_detail(
    identifier: UUID, request: Request, db: Database, user: Authenticated
) -> dict[str, Any]:
    root = owned(db, LearningObjective, user.id, identifier)
    return wrap(request, service.objective_result(db, user.id, service.latest_objective(db, root)))


@router.get("/learning-objectives/{identifier}/versions/{version_id}", response_model=Envelope)
def objective_fixed(
    identifier: UUID, version_id: UUID, request: Request, db: Database, user: Authenticated
) -> dict[str, Any]:
    row = service.objective_version(db, user.id, version_id)
    if row.objective_id != identifier:
        from careerforge.domain.errors import not_found

        raise not_found()
    return wrap(request, service.objective_result(db, user.id, row))


@router.post("/alignment-runs", status_code=202, response_model=Envelope)
def alignment_create(
    body: AlignmentInput, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
) -> dict[str, Any]:
    return wrap(request, service.start_alignment(db, user.id, body, key))


@router.get("/alignment-reports/{identifier}", response_model=Envelope)
def alignment_detail(
    identifier: UUID, request: Request, db: Database, user: Authenticated
) -> dict[str, Any]:
    row = service.alignment_report(db, user.id, identifier)
    return wrap(request, {"alignment_report_id": str(row.id), **row.payload})


@router.get("/plans", response_model=Envelope)
def plan_list(
    request: Request,
    db: Database,
    user: Authenticated,
    cursor: UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = select(LearningPlan).where(LearningPlan.owner_id == user.id)
    if cursor:
        query = query.where(LearningPlan.id > cursor)
    roots = list(db.scalars(query.order_by(LearningPlan.id).limit(limit + 1)))
    data = []
    for root in roots[:limit]:
        row = db.scalar(
            select(LearningPlanVersion)
            .where(LearningPlanVersion.plan_id == root.id)
            .order_by(LearningPlanVersion.version_no.desc())
            .limit(1)
        )
        assert row is not None
        try:
            data.append(service.plan_result(db, user.id, row))
        except BusinessError as error:
            if error.status != 404:
                raise
    return {
        **wrap(request, data),
        "next_cursor": str(roots[limit - 1].id) if len(roots) > limit else None,
    }


@router.get("/plans/{identifier}", response_model=Envelope)
def plan_detail(
    identifier: UUID, request: Request, db: Database, user: Authenticated
) -> dict[str, Any]:
    root = owned(db, LearningPlan, user.id, identifier)
    row = db.scalar(
        select(LearningPlanVersion)
        .where(LearningPlanVersion.plan_id == root.id)
        .order_by(LearningPlanVersion.version_no.desc())
        .limit(1)
    )
    assert row is not None
    return wrap(request, service.plan_result(db, user.id, row))


@router.get("/plans/{identifier}/versions/{version_id}", response_model=Envelope)
def plan_fixed(
    identifier: UUID, version_id: UUID, request: Request, db: Database, user: Authenticated
) -> dict[str, Any]:
    row = owned(db, LearningPlanVersion, user.id, version_id)
    if row.plan_id != identifier:
        from careerforge.domain.errors import not_found

        raise not_found()
    return wrap(request, service.plan_result(db, user.id, row))


@router.get("/plans/{identifier}/diff", response_model=Envelope)
def diff(
    identifier: UUID,
    from_version_id: UUID,
    to_version_id: UUID,
    request: Request,
    db: Database,
    user: Authenticated,
) -> dict[str, Any]:
    return wrap(request, service.plan_diff(db, user.id, identifier, from_version_id, to_version_id))


@router.post("/plans/{identifier}/versions", status_code=201, response_model=Envelope)
def plan_edit(
    identifier: UUID,
    body: PlanRevision,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.edit_plan(db, user.id, identifier, body, key))


@router.post("/plans/{identifier}/adopt", status_code=201, response_model=Envelope)
def plan_adopt(
    identifier: UUID,
    body: PlanAdoption,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.adopt(db, user.id, identifier, body, key))


@router.post(
    "/plans/{identifier}/objectives/{objective_id}/versions",
    status_code=201,
    response_model=Envelope,
)
def plan_objective_edit(
    identifier: UUID,
    objective_id: UUID,
    body: PlanObjectiveRevision,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(
        request, service.edit_plan_objective(db, user.id, identifier, objective_id, body, key)
    )
