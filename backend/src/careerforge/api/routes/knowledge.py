from uuid import UUID

from fastapi import APIRouter, Query, Request

from careerforge.api.dependencies import Authenticated, Database, IdempotencyKey
from careerforge.api.routes.planning import wrap
from careerforge.application import knowledge_service as service
from careerforge.application.ownership import owned
from careerforge.infrastructure.db.models import CodeLearningLink
from careerforge.schemas.knowledge import KnowledgeBind, KnowledgeImport, KnowledgeMetadata
from careerforge.schemas.resources import Envelope

router = APIRouter(prefix="/api/v1/knowledge", tags=["code-knowledge"])


@router.get("/projects", response_model=Envelope)
def projects(request: Request, db: Database, user: Authenticated):
    return wrap(request, service.projects(db, user.id))


@router.post("/imports", status_code=201, response_model=Envelope)
def import_project(
    body: KnowledgeImport, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
):
    return wrap(request, service.import_project(db, user.id, body, key))


@router.post("/snapshots/{identifier}/index", response_model=Envelope)
def index(identifier: UUID, request: Request, db: Database, user: Authenticated):
    return wrap(request, service.build_index(db, user.id, identifier))


@router.put("/snapshots/{identifier}/metadata", response_model=Envelope)
def metadata(
    identifier: UUID, body: KnowledgeMetadata, request: Request, db: Database, user: Authenticated
):
    return wrap(request, service.update_metadata(db, user.id, identifier, body))


@router.get("/search", response_model=Envelope)
def search(
    request: Request,
    db: Database,
    user: Authenticated,
    q: str = Query(min_length=1, max_length=1000),
    snapshot_id: UUID | None = None,
    limit: int = Query(20, ge=1, le=50),
):
    return wrap(request, service.search(db, user.id, q, snapshot_id, limit))


@router.get("/chunks/{identifier}", response_model=Envelope)
def chunk(identifier: UUID, request: Request, db: Database, user: Authenticated):
    return wrap(request, service.chunk_detail(db, user.id, identifier))


@router.get("/chunks/{identifier}/related", response_model=Envelope)
def related(identifier: UUID, request: Request, db: Database, user: Authenticated):
    return wrap(request, service.related(db, user.id, identifier))


@router.post("/chunks/{identifier}/citation", response_model=Envelope)
def citation(identifier: UUID, request: Request, db: Database, user: Authenticated):
    return wrap(request, service.citation(db, user.id, identifier))


@router.post("/links", status_code=201, response_model=Envelope)
def bind(
    body: KnowledgeBind, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
):
    return wrap(request, service.bind(db, user.id, body, key))


@router.get("/links", response_model=Envelope)
def links(objective_version_id: UUID, request: Request, db: Database, user: Authenticated):
    return wrap(request, service.links(db, user.id, objective_version_id))


@router.delete("/links/{identifier}", response_model=Envelope)
def unlink(identifier: UUID, request: Request, db: Database, user: Authenticated):
    row = owned(db, CodeLearningLink, user.id, identifier)
    db.delete(row)
    return wrap(request, {"status": "removed"})
