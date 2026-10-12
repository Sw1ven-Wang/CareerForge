from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from careerforge.api.dependencies import Authenticated, Database, IdempotencyKey
from careerforge.api.routes.planning import wrap
from careerforge.application import repository_service as service
from careerforge.application.common import check_revision, idempotency
from careerforge.application.ownership import owned
from careerforge.application.run_service import db_now
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.db.models import (
    ProjectRecommendation,
    ProjectSelection,
    Repository,
    RepositoryFile,
    RepositorySnapshot,
)
from careerforge.infrastructure.repository.snapshot_reader import catalog
from careerforge.schemas.repositories import (
    ProjectResearchInput,
    RecommendationFeedback,
    RepositoryInput,
    SelectionInput,
)
from careerforge.schemas.resources import Envelope, VersionInput

router = APIRouter(prefix="/api/v1", tags=["repositories"])


@router.get("/repository-catalog", response_model=Envelope)
def curated(request: Request, user: Authenticated) -> dict[str, Any]:
    return wrap(request, [catalog()])


@router.post("/repositories", status_code=201, response_model=Envelope)
def create(
    body: RepositoryInput, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
) -> dict[str, Any]:
    return wrap(request, service.register(db, user.id, body, key))


@router.get("/repositories", response_model=Envelope)
def listing(
    request: Request,
    db: Database,
    user: Authenticated,
    cursor: UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = select(Repository).where(
        Repository.owner_id == user.id, Repository.revoked_at.is_(None)
    )
    if cursor:
        query = query.where(Repository.id > cursor)
    rows = list(db.scalars(query.order_by(Repository.id).limit(limit + 1)))
    data = [
        {
            "repository_id": str(r.id),
            "github_url": r.github_url,
            "requested_ref": r.requested_ref,
            "version": r.revision,
        }
        for r in rows[:limit]
    ]
    return {
        **wrap(request, data),
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }


@router.get("/repositories/{identifier}", response_model=Envelope)
def detail(identifier: UUID, request: Request, db: Database, user: Authenticated) -> dict[str, Any]:
    row = owned(db, Repository, user.id, identifier)
    saved = list(
        db.scalars(
            select(RepositorySnapshot)
            .where(RepositorySnapshot.repository_id == identifier)
            .order_by(RepositorySnapshot.created_at)
        )
    )
    return wrap(
        request,
        {
            "repository_id": str(row.id),
            "github_url": row.github_url,
            "requested_ref": row.requested_ref,
            "snapshots": [
                {"repository_snapshot_id": str(s.id), "commit": s.commit_sha, **s.payload}
                for s in saved
            ],
        },
    )


@router.get("/repository-snapshots/{identifier}", response_model=Envelope)
def snapshot_detail(
    identifier: UUID, request: Request, db: Database, user: Authenticated
) -> dict[str, Any]:
    row = service.snapshot(db, user.id, identifier)
    return wrap(
        request,
        {
            "repository_snapshot_id": str(row.id),
            "repository_id": str(row.repository_id),
            "commit": row.commit_sha,
            "captured_at": row.created_at.isoformat(),
            **row.payload,
        },
    )


@router.get("/repository-snapshots/{identifier}/files", response_model=Envelope)
def files(
    identifier: UUID,
    request: Request,
    db: Database,
    user: Authenticated,
    path: str | None = None,
    symbol: str | None = None,
    cursor: UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    service.snapshot(db, user.id, identifier)
    query = select(RepositoryFile).where(
        RepositoryFile.snapshot_id == identifier, RepositoryFile.owner_id == user.id
    )
    if path:
        query = query.where(RepositoryFile.path.contains(path, autoescape=True))
    if symbol:
        query = query.where(RepositoryFile.content.contains(symbol, autoescape=True))
    if cursor:
        query = query.where(RepositoryFile.id > cursor)
    rows = list(db.scalars(query.order_by(RepositoryFile.id).limit(limit + 1)))
    return {
        **wrap(
            request,
            [
                {
                    "path": r.path,
                    "size": r.size,
                    "content_hash": r.content_hash,
                    "parse_status": r.parse_status,
                }
                for r in rows[:limit]
            ],
        ),
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }


@router.get("/repository-snapshots/{identifier}/file-content", response_model=Envelope)
def file_content(
    identifier: UUID,
    path: str,
    request: Request,
    db: Database,
    user: Authenticated,
    start_line: int = Query(1, ge=1),
    end_line: int = Query(100, ge=1),
) -> dict[str, Any]:
    return wrap(request, service.read_lines(db, user.id, identifier, path, start_line, end_line))


@router.post("/project-research-runs", status_code=202, response_model=Envelope)
def research(
    body: ProjectResearchInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.start_research(db, user.id, body, key))


@router.get("/project-recommendations", response_model=Envelope)
def recommendations(
    request: Request,
    db: Database,
    user: Authenticated,
    run_id: UUID | None = None,
    goal_version_id: UUID | None = None,
    objective_id: UUID | None = None,
    cursor: UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = select(ProjectRecommendation).where(ProjectRecommendation.owner_id == user.id)
    if run_id:
        query = query.where(ProjectRecommendation.run_id == run_id)
    if goal_version_id:
        query = query.where(ProjectRecommendation.goal_version_id == goal_version_id)
    if cursor:
        query = query.where(ProjectRecommendation.id > cursor)
    rows = list(db.scalars(query.order_by(ProjectRecommendation.id).limit(limit + 1)))
    data = []
    for row in rows[:limit]:
        try:
            service.recommendation(db, user.id, row.id)
        except BusinessError as error:
            if error.status == 404:
                continue
            raise
        if objective_id:
            from careerforge.application.planning_service import objective_version

            if not any(
                objective_version(db, user.id, UUID(i)).objective_id == objective_id
                for i in row.payload["objective_version_ids"]
            ):
                continue
        data.append({"recommendation_id": str(row.id), **row.payload})
    return {
        **wrap(request, data),
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }


@router.get("/project-recommendations/{identifier}", response_model=Envelope)
def recommendation_detail(
    identifier: UUID, request: Request, db: Database, user: Authenticated
) -> dict[str, Any]:
    row = service.recommendation(db, user.id, identifier)
    return wrap(request, {"recommendation_id": str(row.id), **row.payload})


@router.post("/project-selections", status_code=201, response_model=Envelope)
def selection(
    body: SelectionInput, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
) -> dict[str, Any]:
    return wrap(request, service.select_project(db, user.id, body, key))


@router.get("/project-selections", response_model=Envelope)
def selections(
    request: Request,
    db: Database,
    user: Authenticated,
    cursor: UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = select(ProjectSelection).where(ProjectSelection.owner_id == user.id)
    if cursor:
        query = query.where(ProjectSelection.id > cursor)
    rows = list(db.scalars(query.order_by(ProjectSelection.id).limit(limit + 1)))
    data = []
    for row in rows[:limit]:
        try:
            service.snapshot(db, user.id, row.snapshot_id)
        except BusinessError as error:
            if error.status == 404:
                continue
            raise
        data.append(
            {
                "selection_id": str(row.id),
                "repository_snapshot_id": str(row.snapshot_id),
                **row.payload,
            }
        )
    return {
        **wrap(request, data),
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }


@router.post(
    "/project-recommendations/{identifier}/feedback", status_code=201, response_model=Envelope
)
def feedback(
    identifier: UUID,
    body: RecommendationFeedback,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return wrap(request, service.feedback(db, user.id, identifier, body, key))


@router.delete("/repositories/{identifier}", status_code=202, response_model=Envelope)
def delete_repository(
    identifier: UUID,
    body: VersionInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    record = idempotency(
        db, user.id, f"repository.{identifier}.delete", key, body.model_dump(mode="json")
    )
    if record.response_summary:
        return wrap(request, record.response_summary)
    row = owned(db, Repository, user.id, identifier, lock=True)
    check_revision(row.revision, body.expected_version)
    row.revoked_at = db_now(db)
    row.revision += 1
    record.response_summary = {
        "repository_id": str(row.id),
        "status": "revoked",
        "version": row.revision,
    }
    return wrap(request, record.response_summary)


@router.get("/repository-snapshots/{identifier}/symbols", response_model=Envelope)
def symbol_lookup(
    identifier: UUID,
    request: Request,
    db: Database,
    user: Authenticated,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    from careerforge.infrastructure.code_index.lexical import rank

    service.snapshot(db, user.id, identifier)
    rows = list(
        db.scalars(
            select(RepositoryFile)
            .where(
                RepositoryFile.snapshot_id == identifier,
                RepositoryFile.owner_id == user.id,
                RepositoryFile.parse_status == "parsed",
            )
            .order_by(RepositoryFile.path)
            .limit(50)
        )
    )
    return wrap(request, rank([{"path": r.path, "content": r.content} for r in rows], q, limit))
