from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from careerforge.api.dependencies import Authenticated, Database, IdempotencyKey
from careerforge.application import market_report_service as service
from careerforge.domain.errors import BusinessError, not_found
from careerforge.infrastructure.db.models import (
    DedupDecision,
    DuplicateCandidate,
    JobPostingVersion,
    MarketReport,
    Requirement,
    RequirementCollection,
)
from careerforge.schemas.job_extraction import (
    DuplicateDecisionInput,
    MarketRunInput,
    RequirementCorrection,
)
from careerforge.schemas.resources import Envelope

router = APIRouter(prefix="/api/v1", tags=["market-research"])


@router.post("/market-research-runs", status_code=202, response_model=Envelope)
def create(
    body: MarketRunInput, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
) -> dict[str, Any]:
    return {"data": service.start(db, user.id, body, key), "request_id": request.state.request_id}


@router.get("/market-reports/{identifier}", response_model=Envelope)
def detail(identifier: UUID, request: Request, db: Database, user: Authenticated) -> dict[str, Any]:
    report = service.read_report(db, user.id, identifier)
    return {
        "data": {"market_report_id": str(report.id), **report.payload},
        "request_id": request.state.request_id,
    }


@router.get("/market-reports", response_model=Envelope)
def listing(
    request: Request,
    db: Database,
    user: Authenticated,
    goal_version_id: UUID | None = None,
    cursor: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    stmt = select(MarketReport).where(MarketReport.owner_id == user.id)
    if goal_version_id:
        stmt = stmt.where(MarketReport.goal_version_id == goal_version_id)
    if cursor:
        stmt = stmt.where(MarketReport.id > cursor)
    rows = list(db.scalars(stmt.order_by(MarketReport.id).limit(limit + 1)))
    data = []
    for row in rows[:limit]:
        try:
            service.read_report(db, user.id, row.id)
        except BusinessError:
            continue
        data.append(
            {
                "market_report_id": str(row.id),
                "goal_version_id": str(row.goal_version_id),
                "run_id": str(row.run_id),
                "counts": row.payload["counts"],
                "synthetic_model": True,
                "created_at": row.created_at.isoformat(),
            }
        )
    return {
        "data": data,
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
        "request_id": request.state.request_id,
    }


@router.post("/requirements/{identifier}/corrections", status_code=201, response_model=Envelope)
def correct(
    identifier: UUID,
    body: RequirementCorrection,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return {
        "data": service.correct(db, user.id, identifier, body, key),
        "request_id": request.state.request_id,
    }


@router.get("/duplicate-candidates", response_model=Envelope)
def candidates(
    request: Request,
    db: Database,
    user: Authenticated,
    goal_version_id: UUID | None = None,
    run_id: UUID | None = None,
    cursor: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    stmt = select(DuplicateCandidate).where(DuplicateCandidate.owner_id == user.id)
    if goal_version_id:
        stmt = stmt.where(DuplicateCandidate.goal_version_id == goal_version_id)
    if run_id:
        stmt = stmt.where(DuplicateCandidate.run_id == run_id)
    if cursor:
        stmt = stmt.where(DuplicateCandidate.id > cursor)
    rows = list(db.scalars(stmt.order_by(DuplicateCandidate.id).limit(limit + 1)))
    data = []
    for c in rows[:limit]:
        postings = [db.get(JobPostingVersion, i) for i in [c.left_id, c.right_id]]
        try:
            service.inputs(db, user.id, [str(p.snapshot_id) for p in postings if p is not None])
        except BusinessError:
            continue
        latest = db.scalar(
            select(DedupDecision)
            .where(DedupDecision.candidate_id == c.id)
            .order_by(DedupDecision.version_no.desc())
            .limit(1)
        )
        data.append(
            {
                "candidate_id": str(c.id),
                "posting_version_ids": [str(c.left_id), str(c.right_id)],
                "reason": c.reason,
                "version": c.revision,
                "latest_decision": {
                    "decision_id": str(latest.id),
                    "decision": latest.decision,
                    "reason": latest.reason,
                }
                if latest
                else None,
            }
        )
    return {
        "data": data,
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
        "request_id": request.state.request_id,
    }


@router.post(
    "/duplicate-candidates/{identifier}/decisions", status_code=201, response_model=Envelope
)
def decide(
    identifier: UUID,
    body: DuplicateDecisionInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return {
        "data": service.decide(db, user.id, identifier, body, key),
        "request_id": request.state.request_id,
    }


@router.get("/requirements/{identifier}", response_model=Envelope)
def requirement_detail(
    identifier: UUID, request: Request, db: Database, user: Authenticated
) -> dict[str, Any]:
    row = db.get(Requirement, identifier)
    if row is None:
        raise not_found()
    collection = db.get(RequirementCollection, row.collection_id)
    assert collection is not None
    posting = db.get(JobPostingVersion, collection.posting_version_id)
    if posting is None or posting.owner_id != user.id:
        raise not_found()
    service.inputs(db, user.id, [str(posting.snapshot_id)])
    replaced = db.scalar(select(Requirement.id).where(Requirement.supersedes_id == row.id))
    return {
        "data": {
            **service.requirement_result(db, row),
            "current_collection_version": collection.revision,
            "superseded_by_requirement_id": str(replaced) if replaced else None,
        },
        "request_id": request.state.request_id,
    }
